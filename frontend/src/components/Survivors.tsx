import { useMemo, useState } from "react";
import { ApiError, api } from "../api/client";
import type { DeliveryTask, MapResponse, Survivor } from "../api/types";
import { Icon } from "./common/Icon";
import { Chip, Empty, Panel, Value, formatTime, type Tone } from "./primitives";

function tone(state: Survivor["state"]): Tone {
  switch (state) {
    case "DELIVERED":
      return "ok";
    case "CONFIRMED":
    case "PENDING_DELIVERY":
    case "ASSIGNED":
    case "DELIVERY_IN_PROGRESS":
      return "bad";
    case "REJECTED":
    case "DUPLICATE":
    case "CANCELLED":
      return "neutral";
    case "LOST":
      return "bad";
    default:
      return "warn";
  }
}

/** Visual family: confirmed (urgent), pending (unverified), delivered (done). */
function family(state: Survivor["state"]): string {
  if (state === "DELIVERED") return "delivered";
  if (["CONFIRMED", "PENDING_DELIVERY", "ASSIGNED", "DELIVERY_IN_PROGRESS"].includes(state))
    return "confirmed";
  if (["DETECTED", "VALIDATING"].includes(state)) return "pending";
  return "";
}

type FilterId = "all" | "review" | "active" | "delivered" | "dismissed";

const FILTERS: { id: FilterId; label: string; match: (s: Survivor) => boolean }[] = [
  { id: "all", label: "All", match: () => true },
  {
    id: "review",
    label: "Review",
    match: (s) => s.state === "DETECTED" || s.state === "VALIDATING",
  },
  {
    id: "active",
    label: "Active",
    match: (s) =>
      ["CONFIRMED", "PENDING_DELIVERY", "ASSIGNED", "DELIVERY_IN_PROGRESS"].includes(s.state),
  },
  { id: "delivered", label: "Delivered", match: (s) => s.state === "DELIVERED" },
  {
    id: "dismissed",
    label: "Dismissed",
    match: (s) => ["DUPLICATE", "REJECTED", "CANCELLED", "LOST"].includes(s.state),
  },
];

/**
 * Survivor tracking.
 *
 * Confidence and position accuracy are two separate, labelled numbers. They are
 * different facts: a 92%-confidence detection may still be located only to
 * +/-25 m, and the delivery aircraft is dispatched against the accuracy.
 * Collapsing them into one figure would be actively misleading.
 *
 * Coordinates come from the mission map GeoJSON (the survivor record itself
 * carries no geometry), so a survivor with no plotted position shows no
 * coordinates rather than a plausible pair.
 */
export function SurvivorsPanel({
  survivors,
  deliveries,
  mapData,
  selectedId,
  onSelect,
  onChanged,
}: {
  survivors: Survivor[];
  deliveries: DeliveryTask[];
  mapData: MapResponse | null;
  selectedId: string | null;
  onSelect: (survivorId: string | null) => void;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<FilterId>("all");

  // survivor_code -> [lon, lat], from the map endpoint's GeoJSON.
  const positions = useMemo(() => {
    const out = new Map<string, [number, number]>();
    for (const feature of mapData?.survivors.features ?? []) {
      if (feature.geometry?.type !== "Point") continue;
      const code = (feature.properties ?? {})["survivor_code"];
      if (typeof code === "string") {
        out.set(code, feature.geometry.coordinates as [number, number]);
      }
    }
    return out;
  }, [mapData]);

  const counts = useMemo(() => {
    const out = {} as Record<FilterId, number>;
    for (const entry of FILTERS) out[entry.id] = survivors.filter(entry.match).length;
    return out;
  }, [survivors]);

  const rows = useMemo(() => {
    const matcher = FILTERS.find((f) => f.id === filter)?.match ?? (() => true);
    return survivors
      .filter(matcher)
      .slice()
      .sort(
        (a, b) =>
          new Date(b.last_observed_at ?? b.first_detected_at).getTime() -
          new Date(a.last_observed_at ?? a.first_detected_at).getTime(),
      );
  }, [survivors, filter]);

  const act = async (survivorId: string, action: "confirm" | "reject") => {
    setBusy(survivorId);
    setError(null);
    try {
      await api.survivorAction(
        survivorId,
        action,
        action === "reject" ? { reason: "Rejected by operator from console" } : {},
      );
      onChanged();
    } catch (cause) {
      setError(cause instanceof ApiError ? `${cause.code}: ${cause.message}` : "Action failed");
    } finally {
      setBusy(null);
    }
  };

  return (
    <Panel
      title="Survivors"
      icon="users"
      actions={<Chip tone={rows.length ? "info" : "neutral"}>{rows.length} SHOWN</Chip>}
      bodyClassName="panel__b--flush"
    >
      <div className="filters">
        {FILTERS.map((entry) => (
          <button
            key={entry.id}
            type="button"
            className={`fchip ${filter === entry.id ? "fchip--on" : ""}`}
            onClick={() => setFilter(entry.id)}
          >
            {entry.label}
            <i>{counts[entry.id]}</i>
          </button>
        ))}
      </div>

      {error ? (
        <div className="callout callout--bad" style={{ margin: 12 }}>
          <Icon name="alert" size={14} className="callout__ic" />
          <div>{error}</div>
        </div>
      ) : null}

      {rows.length === 0 ? (
        <Empty icon="users" title="No survivors detected">
          {survivors.length === 0 ? (
            <>
              Detection events arrive from the companion computers at{" "}
              <code>POST /api/v1/survivors/events</code> and will appear here.
            </>
          ) : (
            "No survivors match this filter."
          )}
        </Empty>
      ) : (
        <div style={{ overflow: "auto", minHeight: 0 }}>
          {rows.map((survivor) => {
            const drone = deliveries.find((d) => d.survivor_id === survivor.id)?.drone_uuid ?? null;
            const actionable = survivor.state === "DETECTED" || survivor.state === "VALIDATING";
            const position = positions.get(survivor.survivor_code);
            return (
              <div
                key={survivor.id}
                className={`srow srow--${family(survivor.state)} ${
                  selectedId === survivor.id ? "srow--sel" : ""
                }`}
                onClick={() => onSelect(selectedId === survivor.id ? null : survivor.id)}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onSelect(selectedId === survivor.id ? null : survivor.id);
                  }
                }}
                title="Click to highlight on the map"
              >
                <span className="srow__av">
                  <Icon name="survivor" size={17} />
                </span>

                <div className="srow__b">
                  <div className="srow__h">
                    <span className="srow__id">{survivor.survivor_code}</span>
                    <Chip tone={tone(survivor.state)}>{survivor.state.replace(/_/g, " ")}</Chip>
                    {drone ? (
                      <Chip tone="vi" icon="delivery">
                        {drone}
                      </Chip>
                    ) : null}
                    <span className="spacer" />
                    <span className="note">{formatTime(survivor.last_observed_at ?? survivor.first_detected_at)}</span>
                  </div>

                  <div className="srow__m">
                    <span>
                      Lat{" "}
                      <b>
                        {position ? position[1].toFixed(4) : <span className="val--missing">--</span>}
                      </b>
                    </span>
                    <span>
                      Lng{" "}
                      <b>
                        {position ? position[0].toFixed(4) : <span className="val--missing">--</span>}
                      </b>
                    </span>
                    <span title="Classifier confidence — not a position accuracy">
                      Confidence{" "}
                      <b>
                        <Value
                          value={
                            survivor.best_confidence === null
                              ? null
                              : survivor.best_confidence * 100
                          }
                          unit="%"
                        />
                      </b>
                    </span>
                    <span title="Position accuracy — the delivery is dispatched against this, not the confidence">
                      Accuracy{" "}
                      <b>
                        ±<Value value={survivor.location_accuracy_m} unit="m" digits={0} />
                      </b>
                    </span>
                  </div>
                </div>

                {actionable ? (
                  <div className="srow__act">
                    <button
                      type="button"
                      className="btn btn--sm btn--cy"
                      disabled={busy === survivor.id}
                      onClick={(e) => {
                        e.stopPropagation();
                        void act(survivor.id, "confirm");
                      }}
                    >
                      Confirm
                    </button>
                    <button
                      type="button"
                      className="btn btn--sm"
                      disabled={busy === survivor.id}
                      onClick={(e) => {
                        e.stopPropagation();
                        void act(survivor.id, "reject");
                      }}
                    >
                      Reject
                    </button>
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}
