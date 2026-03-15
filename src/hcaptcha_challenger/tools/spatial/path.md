# Universal Path & Multi-Piece Strategy

## 1. Global Flow & Inventory
- **Count Pieces ($N$):** Identify exactly how many draggable segments exist on the screen.
- **Trace the Circuit:** Follow the path from "Start" to "Goal" or Nodes 1 → 6. Map every gap.
- **Match Count:** Return exactly $N$ paths.

## 2. Multi-Anchor Grab (Start Points)
- **Left Sidebar Anchor:** If piece is on the left, $X = 120$.
- **Right Sidebar Anchor:** If piece is on the right, $X = 812$.
- **Handle Avoidance:** Target **40px below** the "Move" header to ensure grab.
- **Geometry Match:** Select piece based on angle, curvature, and directional flow.

## 3. Precision Snap (End Points)
- **Node/Road Gaps:** Target the geometric center of the missing segment.
- **X-Mark Grid:** Align block centers with the white "X" markers on the grid.
- **Shortest Line:** Single click center of the smallest color-coded segment.

## 4. Mandatory Syntax
- **Inventory Count:** Must be a literal integer (e.g., 1, 2). NEVER use "N".
- **Safety Bounds:** All coordinates must remain between **50 and 850**.

## 5. Output Schema
{
  "inventory_count": 2,
  "paths": [
    {
      "piece_id": 1,
      "start_point": { "x": 120, "y": 420 },
      "end_point": { "x": 305, "y": 440 }
    },
    {
      "piece_id": 2,
      "start_point": { "x": 812, "y": 510 },
      "end_point": { "x": 450, "y": 360 }
    }
  ]
}