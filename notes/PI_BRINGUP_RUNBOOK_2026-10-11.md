# Pi bring-up runbook — 2026-10-11

Goal: get real readings from the Raspberry Pi into the digital twin (`hnguyen.clustertwin:pi_0`) and check that they arrive. **None of this has been run on the real Pi yet**: the code is tested against the files captured from it on 2026-10-11, not against the live board. Expect to fix small things, and write down what you fix.

How it works, in one paragraph: a small program on the Pi (`pi/pi_sender.py`) reads its CPU load, memory use, temperature and clock speed every 3 seconds and sends them over Wi-Fi to a small program on your laptop (`twin/pi_receiver.py`). The laptop program checks them and writes them into Ditto. The Pi never talks to Ditto and never sees its password. Details: `notes/PI_TELEMETRY_SPEC_2026-10-11.md`.

## 0. Rules to keep

- Do **not** change the Ditto port-forwards to listen on `0.0.0.0` or any LAN address. Ditto still has the default `ditto:ditto` login.
- The shared token is like a password: never put it in a file, a command line argument, a message or git. It travels over plain HTTP, so anyone on the same Wi-Fi network could read it. Use a fresh token for this and nothing else, and use the phone hotspot only with devices you trust on it.
- Stop and remove the firewall rule when you finish (section 9).

## 1. What you need

- Laptop (Windows) with this repo, the `venv`, and PowerShell.
- The Raspberry Pi 3 B+ with its SD card and power supply (5V/2.5A is the official figure).
- Your phone as a Wi-Fi hotspot.
- For the live part (section 8): the Ditto stack running in Minikube with the port-forwards from `scripts\port-forwards.ps1` (see the repo `README.md`).
- Do the "dry run" parts (sections 2–7) first. They do not need Ditto.
- The laptop must stay awake and plugged in for the whole run (turn sleep off in the Windows power settings for the session). If it sleeps, the receiver stops and the sender logs failed sends. It is **not verified** whether your phone's hotspot stays up while the phone screen is locked: watch for disconnects and write them down (section 10).

`<PI_USER>` in the commands below is the user name you chose when you set up the SD card in Raspberry Pi Imager; substitute it yourself (no real user name is written in this file). What this runbook cannot tell you, because it was not recorded, is how the Pi's Wi-Fi is configured.

## 2. Put the laptop and the Pi on the same network

1. Turn on the hotspot on your phone.
2. Connect the **laptop's Wi-Fi** to that hotspot.
3. Power on the Pi. It was set up to join a hotspot and boots to a text console (no desktop). Give it about a minute.
4. On the laptop, find the laptop's own address on the hotspot. In PowerShell:

   ```powershell
   ipconfig
   ```

   Under the Wi-Fi adapter, note the **IPv4 Address**. Call it `LAPTOP_IP` below. It can change every time you reconnect to the hotspot, so check it each session.
5. Find the Pi. Try:

   ```powershell
   ping -4 pitwin.local
   ```

   If that answers, note the address it shows as `PI_IP`. Use the **IPv4** address it prints: the Pi also has IPv6 addresses, but the firewall rule and the receiver use IPv4, so an IPv6 answer would make `PI_IP` wrong. If it does not answer, look at the list of connected devices in your phone's hotspot settings. If the Pi never shows up, it did not join the Wi-Fi; you will need a monitor and keyboard on the Pi to fix that.

   Some phone hotspots stop connected devices from talking to each other (the setting is often called "client isolation"). That is **not verified** for your phone. If section 6 fails with timeouts while the addresses are right, this is the first thing to suspect.

   The hotspot hands out addresses dynamically. If the Pi or the laptop reconnects (or the hotspot is switched off and on), either may get a different address: remove the old firewall rule (section 9, step 3), recreate it with the new `PI_IP` (section 4), and use the new `LAPTOP_IP` in the receiver and sender commands.

## 3. Make the shared token

In PowerShell, in the repo folder:

```powershell
venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
```

Copy the printed text. Then, in the same window:

```powershell
$env:PI_SHARED_TOKEN = Read-Host "Paste the token"
```

(Paste, press Enter.) That variable only exists in this window. You will paste the same token on the Pi in section 5.

## 4. Let the Pi reach the laptop (Windows firewall)

Windows blocks incoming connections by default. Open a **second PowerShell window as Administrator** (the first one keeps your token and runs the receiver) and make a rule that only lets the Pi's address reach port 8765:

```powershell
New-NetFirewallRule -DisplayName "pi-telemetry-receiver" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8765 -RemoteAddress PI_IP -Profile Any
```

Replace `PI_IP` with the **IPv4** address from section 2. If Windows shows an "allow Python on networks" pop-up at any point, read it and allow only what you mean to; this rule already does the job without opening anything wider.

The hotspot assigns addresses dynamically. If the Pi or the laptop reconnects and gets a different address, remove this rule (section 9, step 3) and create it again with the new `PI_IP`, and use the new `LAPTOP_IP` in the receiver and sender commands.

## 5. Dry run: laptop receiver, no Ditto

In the first PowerShell window (token set), from the repo folder:

```powershell
venv\Scripts\python.exe twin\pi_receiver.py --bind LAPTOP_IP --dry-run
```

Expected: a line like `Listening on LAPTOP_IP:8765 thing=hnguyen.clustertwin:pi_0 DRY RUN (no Ditto requests) run log: ...\notes\_pi_runs\receiver_<time>.jsonl`. Even with `--dry-run`, the receiver imports `twin/config.py` and the Ditto client, so start it with the repo's `venv` (it needs `requests` and `python-dotenv`); `twin/config.py` reads the repo's `.env` if there is one. No Ditto request is made. If it says `PI_SHARED_TOKEN must be set`, redo section 3 in this window. Leave it running, and do not click inside this window: a click starts text selection (Windows "QuickEdit"), which pauses the program until you press Esc. The Pi's requests then time out.

Copy the three Pi files over (a **third** PowerShell window, not as Administrator, from the repo folder). Windows 11 includes `ssh` and `scp`:

```powershell
ssh <PI_USER>@pitwin.local "mkdir -p ~/digi-twin/pi ~/digi-twin/twin"
scp pi\pi_sender.py <PI_USER>@pitwin.local:~/digi-twin/pi/
scp twin\__init__.py twin\pi_protocol.py <PI_USER>@pitwin.local:~/digi-twin/twin/
```

(If `pitwin.local` does not resolve, use `PI_IP` instead.) The first connection asks you to type `yes` to trust the Pi.

## 6. Run the sender on the Pi

Log in and set the token **without** typing it into the command history:

```bash
ssh <PI_USER>@pitwin.local
cd ~/digi-twin
read -s -p "Paste the token: " PI_SHARED_TOKEN; export PI_SHARED_TOKEN; echo
python3 pi/pi_sender.py --receiver http://LAPTOP_IP:8765 --count 5
```

What good looks like, on the Pi (times and numbers will differ):

```
… pi_sender session=0123abcd4567ef89 -> LAPTOP_IP:8765 every 3.0s (python 3.13.5)
… seq=0 memory_utilization=31.1 cpu_temperature_c=40.8 cpu_frequency_khz=600000 -> applied
… seq=1 cpu_utilization=1.2 memory_utilization=31.1 … -> applied
…
… done: 5 sent ok, 0 failed
```

`seq=0` has **no** `cpu_utilization`: CPU load needs two readings, so the first one is deliberately left out rather than reported as 0. That is correct.

On the laptop window you should see `POST /v1/probe -> 200` lines (clock checks) and `seq=0 applied ditto=disabled features=3` lines.

If it does not work:

| You see | Likely cause |
|---|---|
| Pi: `send failed … TimeoutError` or `ConnectionRefusedError`, nothing on the laptop | wrong `LAPTOP_IP`, firewall rule missing or wrong `PI_IP`, receiver not running, or hotspot client isolation |
| Pi: `the receiver rejected the token (HTTP 401)` | the two tokens differ; redo section 3 and the `read -s` step with the same text |
| Pi: `ModuleNotFoundError: twin` | the `twin/` files are not at `~/digi-twin/twin/` next to `pi/` |
| Pi: `WARN … unavailable` for a feature | that file could not be read on the Pi; the feature is simply left out, nothing is faked. Write down which one |
| Laptop: nothing prints at all | you are bound to a different address than the Pi is targeting |

## 7. Check the run log

On the laptop, stop the receiver (Ctrl+C), then:

```powershell
venv\Scripts\python.exe scripts\analyze_pi_run.py notes\_pi_runs\receiver_<time>.jsonl
```

(Use the file name the receiver printed; the newest file in `notes\_pi_runs\`.) With only 5 samples most numbers are thin; the point now is that it runs, `applied` is 5, `duplicate`/`stale` are 0, and the clock section found probes. A phone hotspot makes Wi-Fi delay vary a lot, so judge latency only from a longer run (section 8).

## 8. Go live into Ditto

Only after sections 5–7 worked.

1. Start the Ditto stack and port-forwards as usual (repo `README.md`).
2. See what the setup would do (this makes **no** requests):

   ```powershell
   venv\Scripts\python.exe scripts\setup_pi_type.py
   ```

3. If the plan looks right, apply it once. This creates the Twin Type `PiType` and the Thing `pi_0`, outside the rack tree. It needs the policy from the original setup to exist and stops before writing if it does not:

   ```powershell
   venv\Scripts\python.exe scripts\setup_pi_type.py --apply
   ```

   Not verified until you run it: whether the Extended API accepts the empty feature declarations. If it refuses, copy the error message into the notes; do not work around it by seeding zeros.
4. Restart the receiver **without** `--dry-run` (token still set in that window):

   ```powershell
   venv\Scripts\python.exe twin\pi_receiver.py --bind LAPTOP_IP
   ```

5. On the Pi, run the sender for a longer time (no `--count`; stop with Ctrl+C). Ten minutes is a sensible first run:

   ```bash
   python3 pi/pi_sender.py --receiver http://LAPTOP_IP:8765
   ```

6. On the laptop, look at the Thing in Ditto (read-only):

   ```powershell
   venv\Scripts\python.exe -c "from twin.pi_receiver import PI_THING_ID; from twin.ditto_client import ditto, enc; r = ditto('GET', '/api/2/things/' + enc(PI_THING_ID)); print(r.status_code); print(r.text)"
   ```

   You should see `200`, the four feature values, and a `link` feature whose `last_seq` keeps growing and whose `last_received_epoch_ms` is a recent time. Receiver lines should say `ditto=ok`. `ditto=failed` with HTTP 404 means `pi_0` was not created (step 3).
7. Sanity-check the numbers against the Pi itself, once: on the Pi, `cat /sys/class/thermal/thermal_zone0/temp` should be about 1000 times the temperature the twin shows, and `vcgencmd get_throttled` should print `throttled=0x0` (the meaning of each bit is in section 11 of the spec; anything else means undervoltage or throttling happened and the readings need that caveat).
8. The existing Grafana dashboard does **not** show `pi_0`: its panels only select `node_*`, `rack_*` and `main_cluster`. The Pi is checked through Ditto and the analyzer, not through those panels.

## 9. Stop and clean up

1. Ctrl+C the sender on the Pi, then `sudo shutdown now` and wait for the green light to stop flickering before pulling power.
2. Ctrl+C the receiver on the laptop.
3. Remove the firewall rule (Administrator PowerShell):

   ```powershell
   Remove-NetFirewallRule -DisplayName "pi-telemetry-receiver"
   ```

4. Forget the token in each window: `Remove-Item Env:PI_SHARED_TOKEN` on the laptop; close the SSH session on the Pi.
5. Turn the hotspot off.

## 10. What to write down with every run

The analyzer prints the numbers; you add the conditions, because they change the meaning of the numbers: network = phone hotspot, date and time, whether the laptop was on that hotspot by Wi-Fi, any disconnects you noticed, whether the Pi reported `ntp_synchronized` (the analyzer shows the counts), and anything you had to change to make it work. Keep the `receiver_<time>.jsonl` file with the notes; it is the raw record, and it is git-ignored on purpose.
