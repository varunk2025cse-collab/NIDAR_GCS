import { useState } from "react";
import { ApiError, api } from "../api/client";
import type { DeliveryTask, Survivor } from "../api/types";
import { Badge, Panel, Value, formatTime, type Tone } from "./primitives";

function survivorTone(state: Survivor["state"]): Tone {
  switch (state) {
    case "DELIVERED":
      return "ok";
    case "CONFIRMED":
    case "PENDING_DELIVERY":
    case "ASSIGNED":
    case "DELIVERY_IN_PROGRESS":
      return "info";
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

/**
 * Survivors table.
 *
 * Confidence and position accuracy are shown as two separate numbers, stacked
 * and labelled. They are different facts: a 96%-confidence detection may still
 * be located only to +/-25 m, and the delivery aircraft is dispatched against
 * the accuracy, not the confidence. Collapsing them into one "96%" column would
 * be actively misleading.
 */
export function SurvivorsPanel({
  survivors,
  deliveries,
  onChanged,
}: {
  survivors: Survivor[];
  deliveries: DeliveryTask[];
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const assignedTo = (survivorId: string): string | null => {
    const task = deliveries.find((d) => d.survivor_id === survivorId);
    return task?.drone_uuid ?? null;
  };

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
      actions={<span className="note">{survivors.length} records</span>}
      bodyClassName="panel__body--flush"
    >
      {error ? (
        <div className="login__error" style={{ margin: 10 }}>
          {error}
        </div>
      ) : null}

      {survivors.length === 0 ? (
        <div className="table__empty">
          No survivor records. Detections arrive from the companion computers at
          <br />
          <code>POST /api/v1/survivors/events</code>.
        </div>
      ) : (
        <div style={{ overflow: "auto" }}>
          <table className="table">
            <thead>
              <tr>
                <th>ID</th>
                <th>Position</th>
                <th title="Classifier confidence over position accuracy — two different numbers">
                  Conf / Acc
                </th>
                <th>State</th>
                <th>Assigned</th>
                <th>Seen</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {survivors.map((survivor) => {
                const drone = assignedTo(survivor.id);
                const actionable =
                  survivor.state === "DETECTED" || survivor.state === "VALIDATING";
                return (
                  <tr key={survivor.id}>
                    <td className="table__mono">{survivor.survivor_code}</td>
                    <td className="table__mono" style={{ fontSize: "10px" }}>
                      {/*
                        Coordinates are not in the survivor schema; the map
                        endpoint carries the geometry. Observation count is the
                        useful corroboration signal here.
                      */}
                      {survivor.observation_count} obs
                    </td>
                    <td>
                      <div className="twometric">
                        <span className="twometric__primary">
                          <Value
                            value={
                              survivor.best_confidence === null
                                ? null
                                : survivor.best_confidence * 100
                            }
                            unit="%"
                          />
                        </span>
                        <span
                          className="twometric__secondary"
                          title="Position accuracy — the delivery is dispatched against this, not the confidence"
                        >
                          ±
                          <Value value={survivor.location_accuracy_m} unit="m" digits={0} /> pos
                        </span>
                      </div>
                    </td>
                    <td>
                      <Badge tone={survivorTone(survivor.state)}>
                        {survivor.state.replace(/_/g, " ")}
                      </Badge>
                    </td>
                    <td className="table__mono">{drone ?? "--"}</td>
                    <td className="table__mono" style={{ fontSize: "10px" }}>
                      {formatTime(survivor.last_observed_at ?? survivor.first_detected_at)}
                    </td>
                    <td>
                      {actionable ? (
                        <div style={{ display: "flex", gap: 4 }}>
                          <button
                            type="button"
                            className="btn btn--sm btn--primary"
                            disabled={busy === survivor.id}
                            onClick={() => void act(survivor.id, "confirm")}
                          >
                            Confirm
                          </button>
                          <button
                            type="button"
                            className="btn btn--sm"
                            disabled={busy === survivor.id}
                            onClick={() => void act(survivor.id, "reject")}
                          >
                            Reject
                          </button>
                        </div>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
