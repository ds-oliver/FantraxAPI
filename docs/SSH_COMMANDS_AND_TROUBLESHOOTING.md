# SSH Commands & Troubleshooting

## Overview
- Follow the sections in the order below: prepare your Mac shell, manage the VPS Streamlit service, keep Syncthing healthy, and debug if things break.
- A **tunnel** (`ssh -L local:127.0.0.1:remote host`) forwards a loopback port from the VPS back to your Mac. You benefit from tunnels whenever the remote UI binds only to `127.0.0.1`, preventing direct access from the internet. The tunnel lets your browser hit the service via your local `127.0.0.1` port while SSH securely proxies the traffic.

## Step 1 – Mac shell (`zsh`) prep (fresh restart checklist)
When you boot your Mac and need to rebuild the working environment from scratch:
1. Install any Homebrew dependencies (if you haven’t already):  
   `brew install syncthing streamlit`
2. Start the services you rely on:
   ```
   brew services start syncthing
   brew services start streamlit   # only if you intend to run Streamlit locally
   ```
3. Ensure the tunnels/ports are free:
   ```
   lsof -iTCP -sTCP:LISTEN -nP
   ```
   Use the output (see list above) to choose an unused port (e.g., `12345`) before launching Streamlit locally or opening tunnels.
4. Run `git status -sb` to confirm a clean tree, then push/deploy as needed (see cleanup instructions below).
5. Launch any local Streamlit instance on the chosen port:
   ```
   cd /Users/hogan/FantraxAPI
   PYTHONPATH=/Users/hogan/FantraxAPI streamlit run apps/auth_login/overview.py --server.address 127.0.0.1 --server.port 12345
   ```
6. Open the Syncthing tunnel (`ssh -L 8385:127.0.0.1:8385 fantrax-vps`) and visit `http://127.0.0.1:8385/`.
7. Open the Streamlit tunnel (`ssh -L 8501:127.0.0.1:8501 fantrax-vps`) to access the VPS UI—make sure you already ran the Step 2 routine (`BRANCH=testing /opt/FantraxAPI/bin/pull_restart.sh` plus `sudo systemctl restart fantrax-pull-restart.service`) so the remote Streamlit app is listening before you tunnel it.
1. Enter the repo: `cd /Users/hogan/FantraxAPI`
2. Check git status and interpret the working tree:
   ```
   git status -sb
   ```
   - The “Working tree” message you sometimes see (“Existing runtime artifacts (rules JSON, locks, etc.) remain untracked/dirty…”) means temporary files created by Syncthing or the scripts are present but intentionally untracked. They should not be committed—keep them listed so you know what to clean before a commit.
   - When you plan to commit, clean stale SofaScore/syncthing artifacts with:
     ```
     git clean -fd data/sofascore/lineups data/sofascore/schedules
     git clean -fd data/sofascore/*.lock
     ```
   - After cleaning, rerun `git status -sb` to confirm a clean staging area.
2. Restart/debug local services if needed:
   ```
   brew services restart syncthing
   brew services restart streamlit    # only if you run Streamlit locally
   ```
3. Confirm Syncthing is listening on both GUI ports:
5. The usual ports in use (run `lsof -iTCP -sTCP:LISTEN -nP` to confirm):
   ```
   5000, 7000, 52674 (ControlCenter / rapportd)
   17600, 17603 (Dropbox loopback)
   7265 (Raycast)
   52352, 61791, 63440 (Cursor helpers)
   57512 (Chrome/Google)
   8384 (local Syncthing GUI)
   8385 (VPS Syncthing tunnel listener)
   22000 (Syncthing peer sync)
   ```
   Pick a different port (e.g., `12345` or `18080`) before you start/forward Streamlit so there’s no conflict. Always re-run `lsof` before launching a new tunnel to verify the port is free.
6. Start the Syncthing tunnel to the VPS GUI:
   `ssh -L 8385:127.0.0.1:8385 fantrax-vps`
   ```
   ps -ef | grep syncthing
   lsof -i :8384
   lsof -i :8385
   ```
4. Push your working tree to `testing`:
   ```
   git status -sb
   git add -A
   git commit -m "describe your change"
   git push origin testing
   ```
5. Start the Syncthing tunnel to the VPS GUI:
   `ssh -L 8385:127.0.0.1:8385 fantrax-vps`
   - Visit `http://127.0.0.1:8385/` to see the VPS Syncthing UI.
   - The `sofascore` folder should be receive-only, the `vps device` connected, and `Out of Sync Items` declining.

## Step 2 – Streamlit service & tunnels
### Why `root` vs `hogan` matters on the VPS
- The VPS uses two logical accounts:
  1. `root` manages system services (`systemctl`, network debug, etc.).
  2. `hogan` owns the Syncthing configuration under `/home/hogan/.local/state/syncthing` and is the user `syncthing@hogan` runs as. 
- When you run `syncthing@hogan`, systemd drops privileges into the `hogan` user so Syncthing keeps its state isolated; the service will fail (`217/USER`) if that user doesn’t exist. You should `sudo systemctl start syncthing@hogan` as root but never run Syncthing directly as root. When debugging, switch to `hogan` via `sudo -iu hogan` if you need to inspect its home directory or configuration files.

1. If you run Streamlit locally on another port (e.g., `12345`), start it with the port flag:
   ```
   cd /Users/hogan/FantraxAPI
   PYTHONPATH=/Users/hogan/FantraxAPI streamlit run apps/auth_login/overview.py --server.address 127.0.0.1 --server.port 12345
   ```
   Then tunnel whichever port you used (`12345` in this example) instead of `8501`.
2. Open the Streamlit tunnel: `ssh -L 8501:127.0.0.1:8501 fantrax-vps`
   - The browser now reaches the VPS Streamlit UI through `http://127.0.0.1:8501/conditional_swaps`. If it says “Connection failed with status 0,” re-run the tunnel or restart the service (see below).
2. From the VPS shell (`ssh fantrax-vps`), run the auto-pull/restart script that fetches the `testing` branch before reloading Streamlit:
   ```
   sudo systemctl status fantrax-pull-restart.service
   BRANCH=testing /opt/FantraxAPI/bin/pull_restart.sh
   sudo systemctl restart fantrax-pull-restart.service
   tail -n 40 /opt/FantraxAPI/logs/pull_restart.log
   tail -n 40 /opt/FantraxAPI/logs/streamlit.out
   ```
   - This script executes `git fetch origin testing` internally, replaces the working tree, and restarts the managed Streamlit service, so it’s the recommended way to pull the latest repo changes onto the VPS. There’s otherwise no need to manually run `git pull` on the server; just push locally and rerun the script.
3. Confirm you can tunnel the VPS Streamlit UI after the restart (`ssh -L 8501:127.0.0.1:8501 fantrax-vps`) and visit `http://127.0.0.1:8501/` to ensure the app is live.
3. Confirm Streamlit is actually bound to 127.0.0.1:8501 before relying on the tunnel:
   ```
   sudo lsof -i :8501
   sudo systemctl status fantrax-pull-restart.service
   ```
4. Manual debug run (stop the service first):
   ```
   sudo systemctl stop fantrax-pull-restart.service
   PYTHONPATH=/opt/FantraxAPI /opt/FantraxAPI/.venv/bin/python -m streamlit run apps/auth_login/overview.py --server.address 127.0.0.1 --server.port 8501
   ```
   - Ensure port `8501` is free or the script exits with “Port 8501 is not available.”

## Step 3 – Syncthing & clean-up
1. If your Mac was rebooted, start Syncthing before anything else:
   ```
   brew services start syncthing
   ```
   Then check the local Syncthing GUI (`http://127.0.0.1:8384/`):
   - `sofascore` folder should be send-only.
   - The `vps device` should show `Connected` with at least one TCP socket.
   - Health status should show no warnings (especially no “insufficient space” errors).
2. Check the VPS Syncthing GUI through the tunnel (`http://127.0.0.1:8385/`):
   - Folder appears receive-only, `Out of Sync Items` drops to zero, and the `Last Scan` time aligns with the VPS clock.
3. Clean any temporary SofaScore exports before committing:
   ```
   git clean -fd data/sofascore/lineups data/sofascore/schedules
   ```
4. After a VPS reboot, ensure the remote Syncthing service runs under `hogan`:
   ```
   ssh fantrax-vps
   sudo systemctl start syncthing@hogan
   sudo systemctl status syncthing@hogan
   ```
   Then inspect logs when necessary:
   ```
   ssh fantrax-vps
   sudo systemctl status syncthing@hogan
   journalctl -u syncthing@hogan -n 40
   tail -n 40 /opt/FantraxAPI/data/logs/conditional_runner.log
   ```

## Step 4 – Conditional runner diagnostics (VPS)
- Tail the running log: `tail -n 80 /opt/FantraxAPI/data/logs/conditional_runner.log`
- Force a dry-run for debugging:
  ```
  ssh fantrax-vps
  PYTHONPATH=/opt/FantraxAPI /opt/FantraxAPI/.venv/bin/python scripts/conditional_runner.py --user-id <USER_ID> --dry-run --force-trigger
  ```
- Re-run the restart script for a different branch:
  `ssh fantrax-vps BRANCH=testing /opt/FantraxAPI/bin/pull_restart.sh`

## Troubleshooting
### `ssh fantrax-vps` fails to resolve
- Confirm `~/.ssh/config` defines the `fantrax-vps` alias (HostName, User, IdentityFile).
- Run `ssh -G fantrax-vps` to see the resolved host. If aliasing fails, `ssh 5.78.118.108` works directly.
- If DNS remains unreliable, add `/etc/hosts` entry (`5.78.118.108 fantrax-vps`) or ask your network provider for help.

### `fantrax-pull-restart.service` cannot reach GitHub
- Verify outbound connectivity:
  ```
  ssh fantrax-vps
  ping -c 3 github.com
  curl -I https://github.com/
  ```
- If DNS resolution fails, point to different resolvers (`sudo resolvectl dns eth0 1.1.1.1 1.0.0.1`) and flush (`sudo resolvectl flush-caches`). Contact the VPS provider if networking restrictions block GitHub entirely.
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
  - If the browser on Hetzner or any remote host shows “Connection failed with status 0” when hitting `http://127.0.0.1:8501/conditional_swaps`, it cannot reach the loopback port directly—use the tunnel above (or stop the service) before retrying.
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
  - Streamlit is already managed by `fantrax-pull-restart.service`, so the port will be occupied while that service is running. Run the command above only after `sudo systemctl stop fantrax-pull-restart.service` (or use `systemctl restart …` instead) to avoid the “Port 8501 is not available” error.
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
- **Mac terminal (`zsh`)** – run `ps -ef | grep syncthing` and `lsof -i :8384` / `lsof -i :8385` to verify the locally installed `brew services` instance is bound to both GUI ports. The UI at `http://127.0.0.1:8384/` should show the `sofascore` folder in send-only mode, the remote `vps device` marked `connected` (with at least one TCP connection), and the `Health` panel free of error badges. The tunneled UI at `http://127.0.0.1:8385/` should look identical, because that tunnel is simply forwarding the VPS Syncthing GUI; it should show the same devices and folder states plus an active `Last Scan` timestamp that matches the VPS host clock.
- **VPS terminal (`ssh fantrax-vps`)** – once on the VPS shell, use `systemctl status syncthing@hogan` and `journalctl -u syncthing@hogan -n 40` to ensure the service is `Active: active (running)` and reporting “Ready to synchronize” for the `sofascore` folder. The VPS GUI (accessed through the macOS tunnel above) should display the folder as receive-only with the same `vps device` seen on the Mac and the `Out of Sync Items` counter counting down to zero.
- **How to open the VPS SSH terminal** – run `ssh fantrax-vps` from your Mac `zsh` shell (it uses your SSH configs by default). If you need to keep both shells open simultaneously, launch a second terminal window/tab and reuse that command.
- **Ensuring both GUIs stay up** – on the Mac side, `brew services start syncthing` (or `brew services restart syncthing`) keeps the local `/usr/local/var/log/syncthing.log` live at `http://127.0.0.1:8384`. On the VPS side wrap up `sudo systemctl start syncthing@hogan` (after creating the `hogan` user and copying its `/home/hogan/.local/state/syncthing`) so the service listens on `http://127.0.0.1:8384` inside the tunnel; the termination of either service will leave the UI unreachable until restarted, so make sure both commands finish cleanly before relying on Syncthing syncing.


## Troubleshooting
### `ssh fantrax-vps` resolves to “Could not resolve hostname”
- Confirm the alias exists in `~/.ssh/config` with the host entry (HostName, User, IdentityFile).
- Run `ssh -G fantrax-vps` locally to inspect the resolved hostname; use the IP (`5.78.118.108`) if DNS/aliases fail.
- If DNS continues to fail, add a `/etc/hosts` entry (`5.78.118.108 fantrax-vps`) or reach out to your network admin so the alias resolves consistently.
