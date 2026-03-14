# Point Identification Strategy (Center-Point Focus)

## 1. Localize the Target

- Identify based on structural anomalies. Ignore size/perspective differences.

## 2. Axis-Alignment Verification

1. **Trace X:** Follow vertical line to X-axis labels. Note bounds.
2. **Trace Y:** Follow horizontal line to Y-axis labels. Note bounds.
3. **Interpolate:** Estimate precise unit between labels.

## 3. Mandatory Reasoning

State before JSON:

- "X-Axis Bounds: [Min] to [Max]"
- "Y-Axis Bounds: [Min] to [Max]"

## 4. Final Output Schema

```json
{
  "target_description": "Anomaly description",
  "center_point": { "x": 225, "y": 425 }
}
```
