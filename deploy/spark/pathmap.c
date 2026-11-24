/* pathmap.so: LD_PRELOAD path remapping for a Spark where bwrap cannot start (no user
 * namespaces: sb4ec, Ubuntu 24.04 AppArmor).  spark_run_remote.py's no-sandbox mode builds
 * PLX_PATHMAP from the SAME bind list the bwrap sandbox uses (mounts()), so there is one
 * definition of what /data/OntologyProject, /data/venv, /data/plx-state, /run/postgresql ...
 * mean on the Spark.  Format: "<path in job>=<path on the Spark>=<ro|rw>" joined by ':'.
 * A path under an "ro" entry refuses write opens (EROFS), as the sandbox's read-only bind did.
 * Anything not under an entry is untouched.  Paths are mapped lexically (absolute paths only).
 * Not a security boundary: it keeps the job's own /data paths meaning what they mean on the
 * box; the database role (plx_eval) is what keeps the job read-only. */
#define _GNU_SOURCE
#include <alloca.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dirent.h>
#include <spawn.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

struct ent { char *dst; size_t dl; char *src; int ro; };
static struct ent ents[64];
static int nents = -1;

static struct ent tmp_ents[64];
static char *inj[2];
static char pool[16384];     /* static storage: this can run before the process's allocator is ready */
static size_t pool_n;
static char *take(const char *s, size_t n) {
  if (pool_n + n + 1 > sizeof pool) return NULL;
  char *r = pool + pool_n; memcpy(r, s, n); r[n] = 0; pool_n += n + 1; return r;
}
static void load(void) {
  if (nents >= 0) return;
  int n = 0;
  const char *m = getenv("PLX_PATHMAP");
  while (m && *m && n < 64) {           /* "dst=src=ro|rw" joined by ':' ; no strtok: no hidden state */
    const char *e1 = strchr(m, '='); if (!e1) break;
    const char *e2 = strchr(e1 + 1, '='); if (!e2) break;
    const char *end = strchr(e2 + 1, ':'); size_t el = end ? (size_t)(end - e2 - 1) : strlen(e2 + 1);
    tmp_ents[n].dst = take(m, e1 - m); tmp_ents[n].dl = e1 - m;
    tmp_ents[n].src = take(e1 + 1, e2 - e1 - 1);
    tmp_ents[n].ro = el == 2 && strncmp(e2 + 1, "ro", 2) == 0;
    if (!tmp_ents[n].dst || !tmp_ents[n].src) break;
    n++;
    if (!end) break;
    m = end + 1;
  }
  const char *lp = getenv("LD_PRELOAD"), *pm = getenv("PLX_PATHMAP");
  if (lp && pm) {                       /* what a child must carry to stay under the map */
    char t[PATH_MAX]; snprintf(t, sizeof t, "LD_PRELOAD=%s", lp); inj[0] = take(t, strlen(t));
    if (pool_n + strlen(pm) + 16 < sizeof pool) { memcpy(pool + pool_n, "PLX_PATHMAP=", 12); memcpy(pool + pool_n + 12, pm, strlen(pm) + 1); inj[1] = pool + pool_n; pool_n += 12 + strlen(pm) + 1; }
  }
  for (int i = 0; i < n; i++) ents[i] = tmp_ents[i];
  __sync_synchronize();
  nents = n;
}
__attribute__((constructor)) static void init(void) { load(); }

/* longest matching entry wins */
static const struct ent *find(const char *p) {
  load();
  const struct ent *best = NULL;
  if (!p || p[0] != '/') return NULL;
  for (int i = 0; i < nents; i++) {
    size_t l = ents[i].dl;
    if (strncmp(p, ents[i].dst, l) == 0 && (p[l] == 0 || p[l] == '/') && (!best || l > best->dl)) best = &ents[i];
  }
  return best;
}

/* mapped path in buf (or the original when no entry applies); returns the entry or NULL */
static const struct ent *map(const char *p, char *buf) {
  const struct ent *e = find(p);
  if (!e) return NULL;
  if (snprintf(buf, PATH_MAX, "%s%s", e->src, p + e->dl) >= PATH_MAX) return NULL;
  return e;
}
/* PLX_PATHMAP_LOG=<file>: append every absolute path under /data, /var/lib or /run that the job asked for,
 * found nothing at and no bind covers (what a no-sandbox run is missing; the sandbox shows the same as ENOENT) */
static void miss(const char *p, const struct ent *e, int rc) {
  if (rc >= 0 || errno != ENOENT || e || !p || (strncmp(p, "/data/", 6) && strncmp(p, "/var/lib/", 9) && strncmp(p, "/run/", 5))) return;
  const char *f = getenv("PLX_PATHMAP_LOG"); if (!f) return;
  int keep = errno; static __typeof__(&open) ro_ = NULL; if (!ro_) ro_ = dlsym(RTLD_NEXT, "open");
  int fd = ro_(f, O_WRONLY | O_APPEND | O_CREAT, 0600);
  if (fd >= 0) { write(fd, p, strlen(p)); write(fd, "\n", 1); close(fd); }
  errno = keep;
}
#define M(p) char _b[PATH_MAX]; const struct ent *_e = map(p, _b); const char *_p = _e ? _b : (p); (void)_p
#define REAL(name) static __typeof__(&name) real = NULL; if (!real) real = dlsym(RTLD_NEXT, #name)
#define WRITEFLAGS (O_WRONLY | O_RDWR | O_CREAT | O_TRUNC | O_APPEND)

int open(const char *p, int fl, ...) {
  mode_t mode = 0; va_list ap; va_start(ap, fl); if (fl & (O_CREAT | O_TMPFILE)) mode = va_arg(ap, mode_t); va_end(ap);
  M(p); if (_e && _e->ro && (fl & WRITEFLAGS)) { errno = EROFS; return -1; }
  REAL(open); int rc = real(_p, fl, mode); miss(p, _e, rc); return rc;
}
int open64(const char *p, int fl, ...) {
  mode_t mode = 0; va_list ap; va_start(ap, fl); if (fl & (O_CREAT | O_TMPFILE)) mode = va_arg(ap, mode_t); va_end(ap);
  M(p); if (_e && _e->ro && (fl & WRITEFLAGS)) { errno = EROFS; return -1; }
  REAL(open64); return real(_p, fl, mode);
}
int openat(int d, const char *p, int fl, ...) {
  mode_t mode = 0; va_list ap; va_start(ap, fl); if (fl & (O_CREAT | O_TMPFILE)) mode = va_arg(ap, mode_t); va_end(ap);
  M(p); if (_e && _e->ro && (fl & WRITEFLAGS)) { errno = EROFS; return -1; }
  REAL(openat); int rc = real(d, _p, fl, mode); miss(p, _e, rc); return rc;
}
int openat64(int d, const char *p, int fl, ...) {
  mode_t mode = 0; va_list ap; va_start(ap, fl); if (fl & (O_CREAT | O_TMPFILE)) mode = va_arg(ap, mode_t); va_end(ap);
  M(p); if (_e && _e->ro && (fl & WRITEFLAGS)) { errno = EROFS; return -1; }
  REAL(openat64); return real(d, _p, fl, mode);
}
int creat(const char *p, mode_t m) { return open(p, O_CREAT | O_WRONLY | O_TRUNC, m); }
FILE *fopen(const char *p, const char *mode) {
  M(p); if (_e && _e->ro && strpbrk(mode, "wa+")) { errno = EROFS; return NULL; }
  REAL(fopen); return real(_p, mode);
}
FILE *fopen64(const char *p, const char *mode) {
  M(p); if (_e && _e->ro && strpbrk(mode, "wa+")) { errno = EROFS; return NULL; }
  REAL(fopen64); return real(_p, mode);
}
FILE *freopen(const char *p, const char *mode, FILE *f) { M(p); REAL(freopen); return real(_p, mode, f); }
int stat(const char *p, struct stat *s) { M(p); REAL(stat); int rc = real(_p, s); miss(p, _e, rc); return rc; }
int lstat(const char *p, struct stat *s) { M(p); REAL(lstat); return real(_p, s); }
int stat64(const char *p, struct stat64 *s) { M(p); REAL(stat64); return real(_p, s); }
int lstat64(const char *p, struct stat64 *s) { M(p); REAL(lstat64); return real(_p, s); }
int fstatat(int d, const char *p, struct stat *s, int f) { M(p); REAL(fstatat); return real(d, _p, s, f); }
int fstatat64(int d, const char *p, struct stat64 *s, int f) { M(p); REAL(fstatat64); return real(d, _p, s, f); }
int statx(int d, const char *p, int f, unsigned m, struct statx *s) { M(p); REAL(statx); return real(d, _p, f, m, s); }
int access(const char *p, int m) { M(p); if (_e && _e->ro && (m & W_OK)) { errno = EROFS; return -1; } REAL(access); return real(_p, m); }
int faccessat(int d, const char *p, int m, int f) { M(p); if (_e && _e->ro && (m & W_OK)) { errno = EROFS; return -1; } REAL(faccessat); return real(d, _p, m, f); }
ssize_t readlink(const char *p, char *b, size_t n) { M(p); REAL(readlink); return real(_p, b, n); }
ssize_t readlinkat(int d, const char *p, char *b, size_t n) { M(p); REAL(readlinkat); return real(d, _p, b, n); }
char *realpath(const char *p, char *r) { M(p); REAL(realpath); return real(_p, r); }
DIR *opendir(const char *p) { M(p); REAL(opendir); return real(_p); }
int scandir(const char *p, struct dirent ***l, int (*f)(const struct dirent *), int (*c)(const struct dirent **, const struct dirent **)) { M(p); REAL(scandir); return real(_p, l, f, c); }
int chdir(const char *p) { M(p); REAL(chdir); return real(_p); }
int mkdir(const char *p, mode_t m) { M(p); if (_e && _e->ro) { errno = EROFS; return -1; } REAL(mkdir); return real(_p, m); }
int mkdirat(int d, const char *p, mode_t m) { M(p); if (_e && _e->ro) { errno = EROFS; return -1; } REAL(mkdirat); return real(d, _p, m); }
int unlink(const char *p) { M(p); if (_e && _e->ro) { errno = EROFS; return -1; } REAL(unlink); return real(_p); }
int unlinkat(int d, const char *p, int f) { M(p); if (_e && _e->ro) { errno = EROFS; return -1; } REAL(unlinkat); return real(d, _p, f); }
int rmdir(const char *p) { M(p); if (_e && _e->ro) { errno = EROFS; return -1; } REAL(rmdir); return real(_p); }
int rename(const char *a, const char *b) {
  char b1[PATH_MAX], b2[PATH_MAX]; const struct ent *e1 = map(a, b1), *e2 = map(b, b2);
  if ((e1 && e1->ro) || (e2 && e2->ro)) { errno = EROFS; return -1; }
  REAL(rename); return real(e1 ? b1 : a, e2 ? b2 : b);
}
int chmod(const char *p, mode_t m) { M(p); if (_e && _e->ro) { errno = EROFS; return -1; } REAL(chmod); return real(_p, m); }
int chown(const char *p, uid_t u, gid_t g) { M(p); if (_e && _e->ro) { errno = EROFS; return -1; } REAL(chown); return real(_p, u, g); }
int truncate(const char *p, off_t n) { M(p); if (_e && _e->ro) { errno = EROFS; return -1; } REAL(truncate); return real(_p, n); }
int utimensat(int d, const char *p, const struct timespec t[2], int f) { M(p); if (_e && _e->ro) { errno = EROFS; return -1; } REAL(utimensat); return real(d, _p, t, f); }
int symlink(const char *t, const char *p) { M(p); if (_e && _e->ro) { errno = EROFS; return -1; } REAL(symlink); return real(t, _p); }
void *dlopen(const char *p, int f) { char b[PATH_MAX]; const struct ent *e = p ? map(p, b) : NULL; REAL(dlopen); return real(e ? b : p, f); }
int connect(int fd, const struct sockaddr *a, socklen_t l) {
  REAL(connect);
  if (a && a->sa_family == AF_UNIX && l > sizeof(sa_family_t)) {
    const struct sockaddr_un *u = (const struct sockaddr_un *)a; char b[PATH_MAX];
    if (u->sun_path[0] == '/' && map(u->sun_path, b) && strlen(b) < sizeof(u->sun_path)) {
      struct sockaddr_un n; memset(&n, 0, sizeof n); n.sun_family = AF_UNIX; strcpy(n.sun_path, b);
      return real(fd, (struct sockaddr *)&n, sizeof n);
    }
  }
  return real(fd, a, l);
}
/* the child keeps the map even when its environment was rebuilt (env -i, a filtered environment) */
static char *const *carry(char *const ev[], char **store) {
  load();
  if (!inj[0] || !inj[1]) return ev;
  int n = 0, has0 = 0, has1 = 0;
  for (; ev && ev[n]; n++) {
    if (strncmp(ev[n], "LD_PRELOAD=", 11) == 0) has0 = 1;
    if (strncmp(ev[n], "PLX_PATHMAP=", 12) == 0) has1 = 1;
  }
  if (has0 && has1) return ev;
  for (int i = 0; i < n; i++) store[i] = ev[i];
  int k = n; if (!has0) store[k++] = inj[0]; if (!has1) store[k++] = inj[1];
  store[k] = NULL; return store;
}
#define CARRY(ev) char **_st = (char **)alloca(sizeof(char *) * ((ev) ? envn(ev) + 3 : 3)); char *const *_ev = carry((ev), _st)
static int envn(char *const ev[]) { int n = 0; while (ev && ev[n]) n++; return n; }
int execve(const char *p, char *const av[], char *const ev[]) { M(p); CARRY(ev); REAL(execve); int rc = real(_p, av, _ev); miss(p, _e, rc); return rc; }
int execv(const char *p, char *const av[]) { extern char **environ; return execve(p, av, environ); }
/* libc's execvp searches PATH with its own internal exec, so search here with the mapping */
static int search(const char *f, char *out) {
  if (strchr(f, '/')) { snprintf(out, PATH_MAX, "%s", f); return 1; }
  const char *path = getenv("PATH"); if (!path) path = "/usr/local/bin:/usr/bin:/bin";
  int ok = 0;
  for (const char *d = path; d && !ok; ) {
    const char *end = strchr(d, ':'); size_t dl = end ? (size_t)(end - d) : strlen(d);
    char cand[PATH_MAX], real_[PATH_MAX]; snprintf(cand, sizeof cand, "%.*s/%s", (int)dl, d, f);
    const struct ent *e = map(cand, real_);
    if (access(e ? real_ : cand, X_OK) == 0) { snprintf(out, PATH_MAX, "%s", cand); ok = 1; }
    d = end ? end + 1 : NULL;
  }
  return ok;
}
int execvpe(const char *f, char *const av[], char *const ev[]) {
  char b[PATH_MAX]; if (!search(f, b)) { errno = ENOENT; return -1; }
  return execve(b, av, ev);
}
int execvp(const char *f, char *const av[]) { extern char **environ; return execvpe(f, av, environ); }
int posix_spawn(pid_t *pid, const char *p, const posix_spawn_file_actions_t *fa, const posix_spawnattr_t *at, char *const av[], char *const ev[]) {
  M(p); CARRY(ev); REAL(posix_spawn); return real(pid, _p, fa, at, av, _ev);
}
int posix_spawnp(pid_t *pid, const char *f, const posix_spawn_file_actions_t *fa, const posix_spawnattr_t *at, char *const av[], char *const ev[]) {
  char b[PATH_MAX]; if (!search(f, b)) return ENOENT;
  return posix_spawn(pid, b, fa, at, av, ev);
}
