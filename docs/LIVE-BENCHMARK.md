# Live Gemini benchmark on the prerecorded samples

Each of the 28 synthetic worker clips (7 scenarios × 4 variants) was sent through the live check: Gemini transcribes the audio and compares it with every requirement; the server derives the decision. Expected decisions are the curated labels. Measured through the app's own live-check API on a local server calling Gemini 3.5 Flash on Vertex AI (the same code and model the Cloud Run deployment uses). Synthetic English TTS audio only — this is not a field accuracy measurement.

```json
{
  "measured_at": "2026-10-18T10:18:28+00:00",
  "service": "http://localhost:8002",
  "model": "gemini-3.5-flash",
  "samples": 28,
  "completed": 28,
  "agreement": "28/28",
  "incorrect_samples_blocked": "14/14",
  "false_passes": 0,
  "latency_median_s": 8.1,
  "latency_p90_s": 9.6
}
```

| Scenario | Sample | Expected | Live Gemini | Agrees | Latency (s) |
| --- | --- | --- | --- | --- | --- |
| Cartons and bottles | correct | passed | passed | yes | 6.6 |
| Cartons and bottles | incorrect | discrepancy | discrepancy | yes | 13.3 |
| Cartons and bottles | corrected | passed | passed | yes | 7.6 |
| Cartons and bottles | still_incorrect | discrepancy | discrepancy | yes | 8.9 |
| Check every carton | correct | passed | passed | yes | 7.7 |
| Check every carton | incorrect | discrepancy | discrepancy | yes | 9.0 |
| Check every carton | corrected | passed | passed | yes | 8.1 |
| Check every carton | still_incorrect | discrepancy | discrepancy | yes | 8.2 |
| Label colour and lot | correct | passed | passed | yes | 7.5 |
| Label colour and lot | incorrect | discrepancy | discrepancy | yes | 7.8 |
| Label colour and lot | corrected | passed | passed | yes | 8.0 |
| Label colour and lot | still_incorrect | discrepancy | discrepancy | yes | 8.3 |
| Order and handoff time | correct | passed | passed | yes | 7.6 |
| Order and handoff time | incorrect | discrepancy | discrepancy | yes | 7.2 |
| Order and handoff time | corrected | passed | passed | yes | 9.0 |
| Order and handoff time | still_incorrect | discrepancy | discrepancy | yes | 8.1 |
| Destination and extra actions | correct | passed | passed | yes | 5.9 |
| Destination and extra actions | incorrect | discrepancy | discrepancy | yes | 9.8 |
| Destination and extra actions | corrected | passed | passed | yes | 8.4 |
| Destination and extra actions | still_incorrect | discrepancy | discrepancy | yes | 7.5 |
| Sorting and exclusions | correct | passed | passed | yes | 6.1 |
| Sorting and exclusions | incorrect | discrepancy | discrepancy | yes | 8.7 |
| Sorting and exclusions | corrected | passed | passed | yes | 7.3 |
| Sorting and exclusions | still_incorrect | discrepancy | discrepancy | yes | 8.2 |
| A quantity is still uncertain | correct | passed | passed | yes | 9.6 |
| A quantity is still uncertain | incorrect | discrepancy | discrepancy | yes | 10.6 |
| A quantity is still uncertain | corrected | passed | passed | yes | 8.2 |
| A quantity is still uncertain | still_incorrect | review_required | review_required | yes | 8.7 |
