#!/usr/bin/env node
// Preflight for a signed build: names every missing variable and exits 1, never prints a value.
const NEEDED = {
  win: ["AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "PLX_WIN_PUBLISHER_NAME",
        "PLX_WIN_SIGN_ENDPOINT", "PLX_WIN_SIGN_ACCOUNT", "PLX_WIN_SIGN_PROFILE"],
  mac: ["PLX_MAC_IDENTITY", "APPLE_ID", "APPLE_APP_SPECIFIC_PASSWORD", "APPLE_TEAM_ID"],
};

function missing(platform, env) {
  const names = NEEDED[platform];
  if (!names) throw new Error(`unknown platform "${platform}" (win or mac)`);
  return names.filter((n) => !String(env[n] ?? "").trim());
}

module.exports = { NEEDED, missing };

if (require.main === module) {
  let gone;
  try {
    gone = missing(process.argv[2], process.env);
  } catch (e) {
    console.error(e.message);
    process.exit(2);
  }
  if (gone.length) {
    console.error(`Signed ${process.argv[2]} build not started; set: ${gone.join(", ")}`);
    process.exit(1);
  }
  console.log(`Signing settings for ${process.argv[2]} are all set.`);
}
