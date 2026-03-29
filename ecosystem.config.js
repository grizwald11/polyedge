const fs = require('fs');
const path = require('path');

const envPath = path.join(__dirname, 'config', '.env');
const envVars = {};
fs.readFileSync(envPath, 'utf8').split('\n').forEach(line => {
  const [key, ...val] = line.split('=');
  if (key && !key.startsWith('#')) envVars[key.trim()] = val.join('=').trim();
});

module.exports = {
  apps: [
    {
      name: "polyedge",
      script: "venv/bin/python",
      args: "-m src.main",
      cwd: "/Users/adamgrodin/polyedge",
      interpreter: "none",
      env: envVars,
      out_file: "~/.pm2/logs/polyedge-out.log",
      error_file: "~/.pm2/logs/polyedge-error.log",
      autorestart: true,
      max_restarts: 5,
      min_uptime: "10s",
      restart_delay: 10000,
      watch: false,
      kill_timeout: 30000,
    },
  ],
};
