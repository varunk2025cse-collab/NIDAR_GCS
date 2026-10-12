/** Keyboard reference, opened with `?`. */

const GROUPS: { title: string; rows: [string[], string][] }[] = [
  {
    title: "General",
    rows: [
      [["Ctrl", "K"], "Command palette"],
      [["?"], "This sheet"],
      [["Esc"], "Close overlay"],
      [["R"], "Refresh from the backend"],
      [["T"], "Light / dark theme"],
    ],
  },
  {
    title: "Fleet",
    rows: [
      [["1"], "Select first aircraft"],
      [["2"], "Select second aircraft"],
      [["3"], "Select third aircraft"],
      [["["], "Previous aircraft"],
      [["]"], "Next aircraft"],
    ],
  },
  {
    title: "Map",
    rows: [
      [["F"], "Fullscreen map"],
      [["L"], "Follow selected aircraft"],
      [["Z"], "Zoom to fleet and mission"],
      [["B"], "Cycle basemap"],
    ],
  },
  {
    title: "Mission",
    rows: [
      [["P"], "Pause mission"],
      [["Shift", "A"], "Abort (asks for a reason)"],
      [["Shift", "H"], "Return all to launch"],
    ],
  },
];

export function Shortcuts({ onClose }: { onClose: () => void }) {
  return (
    <div
      className="scrim"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label="Keyboard shortcuts"
    >
      <div className="sheet" onClick={(e) => e.stopPropagation()}>
        <div className="sheet__h">
          <h2 style={{ fontSize: 14, fontWeight: 650 }}>Keyboard shortcuts</h2>
          <button type="button" className="btn btn--sm" onClick={onClose}>
            Close
          </button>
        </div>
        <div className="sheet__b">
          {GROUPS.map((group) => (
            <div key={group.title}>
              <div className="sheet__g">{group.title}</div>
              {group.rows.map(([keys, label]) => (
                <div className="sheet__r" key={label}>
                  <span>{label}</span>
                  <span style={{ display: "flex", gap: 3, flex: "none" }}>
                    {keys.map((k) => (
                      <span className="kbd" key={k}>
                        {k}
                      </span>
                    ))}
                  </span>
                </div>
              ))}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
