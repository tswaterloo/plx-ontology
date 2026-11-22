Old-mail search store on sb4ec (option c, 11-22): every mail missing from the box's search, chunked and
embedded off the box and served from here.
  ~/oldmail-store            Postgres 17 restored from the sb4ec physical backup (socket only, port 55710)
  ~/oldmail-code             git archive of the reviewed commit (oldmail-run.sh / oldmail-embed.sh run from it)
  oldmail-store.service      the store's Postgres (Restart=on-failure, MemoryLow 10G)
  oldmail-build.service      python3 -m operating.embed.oldmail chunk: units of 1,000 mails, recorded as each commits
                             (search.oldmail_units); a restart skips recorded units; exit 2 (refused) / 3 (disk floor)
                             do not restart
  oldmail-embed.service      python3 -m operating.embed remote on this host GPU; started by the build on success (OnSuccess),
                             waits while any unit is unchunked; delete embed-state/search/remote_embed.json at launch
Install: cp *.service ~/.config/systemd/user/ && cp *.sh ~ && systemctl --user daemon-reload
Plan once: python3 -m operating.embed.oldmail plan --dsn "host=$HOME/oldmail-store/run port=55710 dbname=plx"
Undo: systemctl --user disable --now oldmail-embed oldmail-build oldmail-store; rm -rf ~/oldmail-store
  oldmail-watch.timer        every 5 min: restarts a build with no unit for 15 min / an embed with no report for 20 min; watch.log
