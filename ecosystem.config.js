// ============================================================================
// IMPORTANT: PM2 log rotation is NOT configured by default. Without it,
// logs at ~/.pm2/logs/ will grow unbounded and eventually exhaust disk space.
// Run these commands ONCE after deploying to set up log rotation:
//
//   pm2 install pm2-logrotate
//   pm2 set pm2-logrotate:max_size 50M
//   pm2 set pm2-logrotate:retain 5
//   pm2 set pm2-logrotate:compress true
//
// Verify with: pm2 conf pm2-logrotate
// ============================================================================

const fs = require('fs');
const path = require('path');

const envPath = path.join(__dirname, 'config', '.env');
const envVars = {};

// Parse .env file with proper handling of:
// - Quoted values (single and double quotes)
// - Inline comments (# after a value)
// - Empty lines and comment-only lines
// - Values containing '=' characters (join on first '=' only)
if (fs.existsSync(envPath)) {
  fs.readFileSync(envPath, 'utf8').split('\n').forEach(line => {
    // Skip blank lines and comment lines
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) return;

    const eqIdx = trimmed.indexOf('=');
    if (eqIdx === -1) return;

    const key = trimmed.slice(0, eqIdx).trim();
    if (!key) return;

    let val = trimmed.slice(eqIdx + 1);

    // Strip inline comments (# not inside quotes)
    // Simple heuristic: strip from first unquoted '#'
    const commentMatch = val.match(/^([^#"']*(?:"[^"]*"|'[^']*')*[^#"']*)(#.*)?$/);
    if (commentMatch) {
      val = commentMatch[1];
    }

    val = val.trim();

    // Strip surrounding quotes (double or single)
    if ((val.startsWith('"') && val.endsWith('"')) ||
        (val.startsWith("'") && val.endsWith("'"))) {
      val = val.slice(1, -1);
    }

    envVars[key] = val;
  });
}

module.exports = {
  apps: [
    {
      name: "polyedge",
      script: "venv/bin/python",
      args: "-m src.main",
      cwd: __dirname,
      interpreter: "none",
      env: envVars,
      out_file: "~/.pm2/logs/polyedge-out.log",
      error_file: "~/.pm2/logs/polyedge-error.log",
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      // L-3: Log rotation — run these commands once to set up:
      //   pm2 install pm2-logrotate
      //   pm2 set pm2-logrotate:max_size 50M
      //   pm2 set pm2-logrotate:retain 5
      //   pm2 set pm2-logrotate:compress true
      // Without logrotate, PM2 logs at ~/.pm2/logs/ will grow unbounded.
      autorestart: true,
      max_restarts: 15,
      min_uptime: "10s",
      restart_delay: 10000,
      watch: false,
      kill_timeout: 60000,
      max_memory_restart: "500M",
    },
  ],
};
