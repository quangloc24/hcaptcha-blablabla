# Bounding Box (BBox) Extraction Strategy

## 1. Tight-Fit Constraint

- Minimal area encompassing the target object only. Exclude UI text/buttons.

## 2. Coordinate Mapping

Read boundaries directly from grid labels:

- **top_left_x / y**: Grid lines immediately Left/Above the object.
- **bottom_right_x / y**: Grid lines immediately Right/Below the object.

## 3. Validation

- Ensure $bottom\_right\_x > top\_left\_x$ and $bottom\_right\_y > top\_left\_y$.

## 4. Final Output Schema

```json
{
  "challenge_prompt": "{task_instructions}",
  "bounding_box": {
    "top_left_x": 148,
    "top_left_y": 260,
    "bottom_right_x": 235,
    "bottom_right_y": 345
  }
}
```
