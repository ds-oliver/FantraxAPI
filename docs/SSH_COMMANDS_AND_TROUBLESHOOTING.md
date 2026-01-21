# SSH Commands & Troubleshooting

## Mac zsh commands
- Start the local Streamlit UI (mirrors VPS runtime):
  `cd /Users/hogan/FantraxAPI && streamlit run apps/auth_login/overview.py --server.address 127.0.0.1 --server.port 8501`
- Push the codebase to the `testing` branch:
  ```
  cd /Users/hogan/FantraxAPI
  git status -sb
  git add -A
  git commit -m "your message"
  git push origin testing
  ```
- Trigger the VPS `fantrax-pull-restart.service` so it pulls and restarts Streamlit:
  `ssh fantrax-vps sudo systemctl restart fantrax-pull-restart.service`
- Tunnel the VPS Syncthing UI for diagnostics:
  `ssh -L 8385:127.0.0.1:8385 fantrax-vps`
- Tunnel the deployed Streamlit UI to preview production:
  `ssh -L 8501:127.0.0.1:8501 fantrax-vps`
- Clean up Syncthing conflict artifacts before committing:
  ```
  cd /Users/hogan/FantraxAPI
  git clean -fd data/sofascore/lineups data/sofascore/schedules
  ```

## VPS shell commands
- SSH into the VPS console: `ssh fantrax-vps`
- Manually start Streamlit on the VPS for debugging:
  ```
  cd /opt/FantraxAPI
  PYTHONPATH=/opt/FantraxAPI /opt/FantraxAPI/.venv/bin/python -m streamlit run apps/auth_login/overview.py --server.address 127.0.0.1 --server.port 8501
  ```
- Inspect the `fantrax-pull-restart.service` and its logs:
  ```
  sudo systemctl status fantrax-pull-restart.service
  tail -n 40 /opt/FantraxAPI/logs/pull_restart.log
  tail -n 40 /opt/FantraxAPI/logs/streamlit.out
  ```
- Check the conditional runner activity:
  `tail -n 80 /opt/FantraxAPI/data/logs/conditional_runner.log`
- Run a forced dry-run of the conditional runner for a user:
  ```
  PYTHONPATH=/opt/FantraxAPI /opt/FantraxAPI/.venv/bin/python scripts/conditional_runner.py --user-id <USER_ID> --dry-run --force-trigger
  ```
- Re-run the auto-restart script for a specific branch:
  `BRANCH=testing /opt/FantraxAPI/bin/pull_restart.sh`

## Syncthing connection

## Troubleshooting
### `ssh fantrax-vps` resolves to “Could not resolve hostname”
- Confirm the alias exists in `~/.ssh/config` with the host entry (HostName, User, IdentityFile).
- Run `ssh -G fantrax-vps` locally to inspect the resolved hostname; use the IP (`5.78.118.108`) if DNS/aliases fail.
- If DNS continues to fail, add a `/etc/hosts` entry (`5.78.118.108 fantrax-vps`) or reach out to your network admin so the alias resolves consistently.
