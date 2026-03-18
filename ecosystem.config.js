module.exports = {
  apps: [
    {
      name: "polyedge",
      script: "venv/bin/python",
      args: "-m src.main",
      cwd: "/Users/adamgrodin/polyedge",
      interpreter: "none",
      env_file: "config/.env",
      out_file: "~/.pm2/logs/polyedge-out.log",
      error_file: "~/.pm2/logs/polyedge-error.log",
      autorestart: true,
      max_restarts: 10,
      watch: false,
      kill_timeout: 5000,
    },
  ],
};
