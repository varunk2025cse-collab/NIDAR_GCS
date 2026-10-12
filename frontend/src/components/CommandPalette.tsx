/**
 * Command palette (Ctrl/Cmd+K).
 *
 * Every entry maps to something the console can genuinely do -- a real REST
 * command, a real view change. Destructive flight actions keep their
 * confirmation prompts here exactly as they have them on the buttons: the
 * palette is a faster route to the same audited call, never a way around it.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { Icon, type IconName } from "./common/Icon";

export interface Command {
  id: string;
  group: string;
  label: string;
  icon?: IconName;
  hint?: string;
  disabled?: boolean;
  run: () => void | Promise<void>;
}

export function CommandPalette({
  open,
  commands,
  onClose,
}: {
  open: boolean;
  commands: Command[];
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (open) {
      setQuery("");
      setIndex(0);
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    const pool = commands.filter((c) => !c.disabled);
    if (!q) return pool;
    // Subsequence match, so "rtl" finds "Return all aircraft to launch".
    return pool.filter((c) => {
      const haystack = `${c.group} ${c.label}`.toLowerCase();
      let i = 0;
      for (const ch of q) {
        i = haystack.indexOf(ch, i);
        if (i === -1) return false;
        i += 1;
      }
      return true;
    });
  }, [commands, query]);

  useEffect(() => {
    if (index >= matches.length) setIndex(0);
  }, [matches.length, index]);

  useEffect(() => {
    listRef.current?.querySelector(".cmd__i--on")?.scrollIntoView({ block: "nearest" });
  }, [index]);

  if (!open) return null;

  const grouped: { group: string; items: Command[] }[] = [];
  for (const command of matches) {
    const last = grouped.at(-1);
    if (last && last.group === command.group) last.items.push(command);
    else grouped.push({ group: command.group, items: [command] });
  }

  const choose = (command: Command | undefined) => {
    if (!command) return;
    onClose();
    void command.run();
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setIndex((i) => Math.min(matches.length - 1, i + 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setIndex((i) => Math.max(0, i - 1));
    } else if (event.key === "Enter") {
      event.preventDefault();
      choose(matches[index]);
    } else if (event.key === "Escape") {
      event.preventDefault();
      onClose();
    }
  };

  let flat = -1;

  return (
    <div
      className="scrim"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label="Command palette"
    >
      <div className="cmd" onClick={(e) => e.stopPropagation()}>
        <input
          ref={inputRef}
          className="cmd__in"
          placeholder="Search commands…  (navigate, aircraft, mission, map)"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setIndex(0);
          }}
          onKeyDown={onKeyDown}
          aria-label="Search commands"
        />

        <div className="cmd__l" ref={listRef}>
          {grouped.length === 0 ? (
            <div className="empty">
              <div className="empty__t">No matching command</div>
            </div>
          ) : (
            grouped.map((section) => (
              <div key={section.group}>
                <div className="cmd__g">{section.group}</div>
                {section.items.map((command) => {
                  flat += 1;
                  const active = flat === index;
                  const myIndex = flat;
                  return (
                    <button
                      key={command.id}
                      type="button"
                      className={`cmd__i ${active ? "cmd__i--on" : ""}`}
                      onMouseEnter={() => setIndex(myIndex)}
                      onClick={() => choose(command)}
                    >
                      <span className="cmd__ic">
                        <Icon name={command.icon ?? "chevron"} size={15} />
                      </span>
                      <span className="cmd__lab">{command.label}</span>
                      {command.hint ? <span className="cmd__hint">{command.hint}</span> : null}
                    </button>
                  );
                })}
              </div>
            ))
          )}
        </div>

        <div className="cmd__f">
          <span>
            <span className="kbd">↑</span> <span className="kbd">↓</span> navigate
          </span>
          <span>
            <span className="kbd">↵</span> run
          </span>
          <span>
            <span className="kbd">esc</span> close
          </span>
          <span className="spacer" />
          <span>{matches.length} commands</span>
        </div>
      </div>
    </div>
  );
}
