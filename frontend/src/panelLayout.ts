export type PanelSize = { width: number; height: number }

// Keep 12px side/top margins and 44px below the panel for map attribution.
export function fitPanelSize(size: PanelSize, bounds: PanelSize): PanelSize {
  return {
    width: Math.min(Math.max(280, size.width), Math.max(0, bounds.width - 24)),
    height: Math.min(Math.max(220, size.height), Math.max(0, bounds.height - 56)),
  }
}

export function resizePanel(size: PanelSize, dx: number, dy: number, side: 'left' | 'right', bounds: PanelSize): PanelSize {
  return fitPanelSize({ width: size.width + dx * (side === 'right' ? -1 : 1), height: size.height + dy }, bounds)
}
