# Monoprice Blackbird 4x4 HDMI Matrix for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Validate](https://github.com/thrivestorm2/blackbird4/actions/workflows/validate.yml/badge.svg)](https://github.com/thrivestorm2/blackbird4/actions/workflows/validate.yml)

Control a **Monoprice Blackbird 4K 4x4 HDMI matrix** (developed against PN 39668) from Home Assistant over your local network. Home Assistant's built-in `blackbird` integration only supports the 8x8 model; this one supports the 4x4.

It talks to the same CGI endpoints the matrix's own web page uses, so you don't need a serial cable. If your unit's web interface doesn't answer, it falls back to the raw TCP command port.

## Features

- UI setup (config flow): enter the matrix's IP address.
- One **media player** per output (`media_player.blackbird_4x4_output_1` …), whose *source* is the input routed to it.
- One **select** per output (`select.blackbird_4x4_output_1_source` …), handy for dashboards and automations.
- Custom names for inputs and outputs, e.g. "Apple TV" → "Living Room TV".
- Services:
  - `blackbird4.set_all_outputs`: route one input to every output.
  - `blackbird4.send_command`: send any raw command in your matrix's command set (`r av out 0!`, `PowerOFF.`, …) and get the reply back.
- Local polling (every 10 s by default). Entities go unavailable if the matrix stops answering.
- Diagnostics download that includes the raw status reply, useful for bug reports.
- Bundled integration icon, shown on Home Assistant 2026.3 and later.

## Installation

### HACS (recommended)

1. In Home Assistant, open **HACS → ⋮ → Custom repositories**.
2. Add `https://github.com/thrivestorm2/blackbird4` with category **Integration**.
3. Search for **Monoprice Blackbird 4x4 HDMI Matrix**, install it, and restart Home Assistant.

### Manual

Copy `custom_components/blackbird4` into your Home Assistant `config/custom_components/` folder and restart.

## Setup

**Settings → Devices & services → Add integration → Monoprice Blackbird 4x4 HDMI Matrix**, then enter:

| Field | Value |
| --- | --- |
| Host | The matrix's IP address, e.g. `192.168.1.39`. Give it a DHCP reservation so it doesn't change. |
| Port | `80` (the web interface) |

To rename inputs and outputs or change the polling interval, open the integration's **Configure** menu. To change the IP later, use **Reconfigure**.

## Examples

```yaml
# Send the Apple TV (input 2) to the bedroom (output 3)
action: select.select_option
target:
  entity_id: select.blackbird_4x4_output_3_source
data:
  option: Apple TV

# Everything shows input 1
action: blackbird4.set_all_outputs
data:
  input: 1

# Raw command, with the reply returned
action: blackbird4.send_command
data:
  command: "Status."
response_variable: reply
```

## Troubleshooting

If setup fails with *cannot connect* or *unknown protocol*, or the routes look wrong, run the probe script from any computer on the same network (Python 3, nothing to install):

```sh
curl -O https://raw.githubusercontent.com/thrivestorm2/blackbird4/main/scripts/probe.py
python3 probe.py 192.168.1.39            # read-only
python3 probe.py 192.168.1.39 --switch   # also routes input 1 to output 1
```

Open an issue and paste the output. It shows exactly what your firmware's web interface and TCP ports reply, so support for it can be added.

To get debug logs:

```yaml
logger:
  logs:
    custom_components.blackbird4: debug
```

## How it works

Blackbird matrices are rebadged OEM hardware, and the protocol depends on the firmware. Setup tries each of these and keeps the first one that answers:

| Protocol | Read routing | Switch |
| --- | --- | --- |
| `http` | `POST /cgi-bin/MUH44TP_getsetparams.cgi`, body `lcc`. The reply is `({'Outputbuttom': ['1','2','3','4'], ...})`, where entry *n* is the input routed to output *n*. | `POST /cgi-bin/MMX32_Keyvalue.cgi`, body `{CMD=OUT03:02.` |
| `web_hex` (PN 39668, web UI "B44H70") | `GET /cgi-bin/submit?cmd=hex(a5,5b,02,01,<out>,00,…,<sum>)` per output, then `GET /cgi-bin/query`. The reply `hex(a5,5b,02,01,<out>,00,<in>,…)` carries the input in byte 7. | `hex(a5,5b,02,03,<in>,00,<out>,…)`. The byte order is checked on the first switch, because vendor sheets disagree. |
| `web_terminal` | `GET /cgi-bin/instr?cmd=r+av+out+0!%0D`, then `GET /cgi-bin/query` for the reply, e.g. `input 2 -> output 1` | `s in 2 av out 1!` (falls back to `s 2 av 1!`) |
| `json` | `POST /cgi-bin/instr`, body `{"comhead":"get video status","language":0}`. The reply's `allsource` list gives the input for each output. | `{"comhead":"video switch","language":0,"source":[3,2]}` |
| `tcp` | `Status.` on TCP port 23/4001/8000/5000 | `OUT03:02.` |
| `tcp_bang` | `r av out 0!` on TCP port 8000/23/4001/5000 | `s in 2 av out 1!` |

If none of them answer, the setup form shows a probe report quoting the request code from the matrix's own web UI scripts.

The matrix's web server sends malformed HTTP, so the integration uses a small raw-socket HTTP client and needs no third-party Python packages. Credit to [alexyao2015/hass-blackbird24180](https://github.com/alexyao2015/hass-blackbird24180) and [ryan-haver/8x8-hdmi-matrix-hub](https://github.com/ryan-haver/8x8-hdmi-matrix-hub) for documenting the `http` and `json` protocols.

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
ruff check . && ruff format --check . && pytest
```

Release checklist: bump `version` in `custom_components/blackbird4/manifest.json`, tag `vX.Y.Z`, and publish a GitHub release (HACS offers releases as versions).

For HACS validation to pass, set the GitHub repository's **description** and **topics** (`home-assistant`, `hacs`, `hacs-integration`, `monoprice`, `hdmi-matrix`) and keep **Issues** enabled.

## License

MIT
