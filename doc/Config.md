# CarConnectivity Connector for Lucid — config options

```json
{
    "carConnectivity": {
        "connectors": [
            {
                "type": "lucid",
                "disabled": false,
                "config": {
                    "log_level": "error",
                    "api_log_level": "error",
                    "interval": 60,
                    "refresh_token_file": "~/.config/lucid-token.json",
                    "hide_vins": [],
                    "commands": false
                }
            }
        ]
    }
}
```

| option | default | meaning |
|---|---|---|
| `refresh_token_file` | required | Path to a JSON file `{"refresh_token": "…"}`. Never the token itself. |
| `interval` | 60 | Seconds between polls of a car that is awake and parked or charging. Minimum 60. One gRPC call per poll returns every vehicle. |
| `log_level` / `api_log_level` | error | Standard CarConnectivity log levels. |
| `hide_vins` | `[]` | VINs to ignore. |
| `driving_interval` | `interval` | Seconds between polls while a car is driving. Minimum 15, at most `interval`. |
| `sleep_interval` | `240` (or `interval`, if longer) | Seconds between polls while every car is asleep. At least `interval`, at most 900. A wake shows on the first read after it, so this is the longest it goes unseen. |
| `commands` | `false` | `true` (the JSON boolean, nothing else) gives each vehicle commands: charging, charge limit, AC current limit, climatization, defrost, locks, trunk, frunk, windows, lights and horn, headlights, wake, Sentry and the alarm, seat and steering wheel heat, Max A/C, creature comfort, battery preconditioning. Off, the connector is read-only and exposes no command. See the README's Commands section. |
