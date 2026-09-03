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
                    "hide_vins": []
                }
            }
        ]
    }
}
```

| option | default | meaning |
|---|---|---|
| `refresh_token_file` | required | Path to a JSON file `{"refresh_token": "…"}`. Never the token itself. |
| `interval` | 60 | Seconds between polls. Minimum 60. One gRPC call per poll returns every vehicle. Keep under 240 so a refresh happens inside each 5-minute session. |
| `log_level` / `api_log_level` | error | Standard CarConnectivity log levels. |
| `hide_vins` | `[]` | VINs to ignore. |
