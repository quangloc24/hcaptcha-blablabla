# Path Completion Strategy (Multi-Piece & Flow Logic)

## 1. Inventory & Global Flow

- **Count Pieces:** Identify exactly how many draggable pieces are on the RIGHT ($N$).
- **Trace the Circuit:** Follow the path from the "Start" character to the "Goal." Identify every gap in between.
- **Match Count:** You must return exactly $N$ paths.

## 2. Match by Geometry & Connectivity

For each gap, the correct piece must:

- **Angle/Curvature:** Match the endpoints of the break.
- **Directional Flow:** Have an "entrance" that fits the previous segment and an "exit" that fits the next segment.
- **Length:** Be the correct size to bridge the gap.

## 3. Coordinate Calculation (Oxy Labels)

- **IMPORTANT:** Read coordinates directly from numeric axis labels. Do not estimate via pixels.
- **FROM (Start):** The center $(x, y)$ of the piece on the RIGHT.
- **TO (End):** The center $(x, y)$ of the gap on the LEFT/CENTER.

## 4. Anti-Hallucination Rules

- If there are $N$ pieces, return exactly $N$ paths.
- **Direction:** `start_point` must have higher X (Right) than `end_point` (Left).
- Never move a piece to the same side of the screen.

## 5. Output Schema

```json
{
  "inventory_count": 3,
  "paths": [
    {
      "piece_id": 1,
      "start_point": { "x": 800, "y": 200 },
      "end_point": { "x": 200, "y": 400 }
    },
    {
      "piece_id": 2,
      "start_point": { "x": 800, "y": 400 },
      "end_point": { "x": 400, "y": 600 }
    }
  ]
}
```
