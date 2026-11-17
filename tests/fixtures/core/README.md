# core fixtures

Real responses, captured on pc-desktop on 2026-11-09. Nothing here is hand-written.

| File | Origin |
|---|---|
| `lacounty_fire_stations_2026-11-09.geojson` | LA County eGIS LMS layer 137 `/query`, `where=org_name = 'Los Angeles County Fire Department'` |
| `lacounty_hospitals_2026-11-09.geojson` | LA County eGIS LMS layer 81 `/query` |
| `lacounty_heliports_2026-11-09.geojson` | LA County eGIS LMS layer 183 `/query` |
| `lacounty_fhsz_34.2000_-118.2000_2026-11-09.geojson` | LA County Hazards MapServer/2 point query (`Very High`) |
| `lacounty_fhsz_34.1478_-118.1445_2026-11-09.geojson` | same layer, point query at Griffith Observatory (`NonWildland`) |
| `kodros_chat_completion_2026-11-09.json` | `POST http://127.0.0.1:8080/v1/chat/completions` on the Kodros cluster |
| `kodros_models_2026-11-09.json` | `GET http://127.0.0.1:8080/v1/models` on the same cluster |

## Kodros cluster captures

Both bodies came from the live local cluster, model alias `cluster-current`. The cluster reported
`"model": "DeepSeek-V4.1-Flash-1017-EXL3"` in the completion and listed that id with
`"loaded_model": "DeepSeek-V4.1-Flash-1017-EXL3"` behind the alias in `/models`. That server-reported
id is what `core.llm.chat_result().model` exists to record.

The completion request (kept here so the capture is reproducible) was:

```json
{"model": "cluster-current",
 "messages": [{"role": "system", "content": "You are a wildfire intelligence analyst. Answer in one short sentence."},
              {"role": "user", "content": "Which Los Angeles County city is nearest to the 2025 Eaton Fire ignition area? Answer in one sentence."}],
 "max_tokens": 700, "temperature": 0, "stream": false}
```

It returned `finish_reason: "stop"`, `usage.total_tokens: 293` (207 of them `reasoning_tokens`) after
about 56 s on the cluster. The test replays the body from a local stdlib HTTP server, so no test needs
the cluster to be up.
