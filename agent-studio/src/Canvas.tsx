import { useRef, useState } from "react";
import { accentOf, type Edge, type TeamMember } from "./api";

const W = 132, H = 44;

export function Canvas({
  team, edges, onMove, onToggleEdge,
}: {
  team: TeamMember[];
  edges: Edge[];
  onMove: (id: string, x: number, y: number) => void;
  onToggleEdge: (source: string, target: string) => void;
}) {
  const ref = useRef<SVGSVGElement>(null);
  const [pos, setPos] = useState<Record<string, { x: number; y: number }>>({});
  const [drag, setDrag] = useState<{ id: string; dx: number; dy: number } | null>(null);
  const [link, setLink] = useState<string | null>(null);

  const at = (m: TeamMember) => pos[m.id] || { x: m.x, y: m.y };

  const toLocal = (e: React.PointerEvent) => {
    const r = ref.current!.getBoundingClientRect();
    return {
      x: ((e.clientX - r.left) / r.width) * 800,
      y: ((e.clientY - r.top) / r.height) * 500,
    };
  };

  const down = (e: React.PointerEvent, m: TeamMember) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    const p = toLocal(e);
    const c = at(m);
    setDrag({ id: m.id, dx: p.x - c.x, dy: p.y - c.y });
  };

  const move = (e: React.PointerEvent) => {
    if (!drag) return;
    const p = toLocal(e);
    setPos((s) => ({
      ...s,
      [drag.id]: {
        x: Math.max(W / 2, Math.min(800 - W / 2, p.x - drag.dx)),
        y: Math.max(H / 2, Math.min(500 - H / 2, p.y - drag.dy)),
      },
    }));
  };

  const up = (m: TeamMember) => {
    if (drag?.id === m.id) {
      const c = at(m);
      if (Math.abs(c.x - m.x) > 1 || Math.abs(c.y - m.y) > 1) onMove(m.id, c.x, c.y);
      else clickNode(m);
    }
    setDrag(null);
  };

  const clickNode = (m: TeamMember) => {
    if (!link) setLink(m.id);
    else if (link === m.id) setLink(null);
    else { onToggleEdge(link, m.id); setLink(null); }
  };

  const path = (a: { x: number; y: number }, b: { x: number; y: number }) => {
    const my = (a.y + b.y) / 2;
    return `M ${a.x} ${a.y + (b.y > a.y ? H / 2 : -H / 2)} C ${a.x} ${my}, ${b.x} ${my}, ${b.x} ${b.y + (b.y > a.y ? -H / 2 : H / 2)}`;
  };

  return (
    <>
      <svg ref={ref} className="canvas" viewBox="0 0 800 500" preserveAspectRatio="xMidYMid meet" onPointerMove={move}>
        {edges.map((e) => {
          const s = team.find((t) => t.id === e.source_agent_id);
          const t = team.find((x) => x.id === e.target_agent_id);
          if (!s || !t) return null;
          return <path key={e.id} className={"edge " + e.kind} d={path(at(s), at(t))} />;
        })}
        {team.map((m) => {
          const c = at(m);
          const role = m.team_role === "orchestrator" ? "chef d'orchestre"
            : m.team_role === "critic" ? "critique" : "spécialiste";
          return (
            <g key={m.id} className="node-box" transform={`translate(${c.x - W / 2} ${c.y - H / 2})`}
              onPointerDown={(e) => down(e, m)} onPointerUp={() => up(m)}>
              <rect className={"node-r" + (link === m.id ? " sel" : "")} width={W} height={H} rx={9} />
              <rect width={3.5} height={H} rx={2} fill={accentOf(m.accent)} />
              <text className="node-t" x={14} y={19}>{m.name.slice(0, 17)}</text>
              <text className="node-s" x={14} y={33}>{role}</text>
            </g>
          );
        })}
      </svg>
      <div className="small muted" style={{ marginTop: 8 }}>
        Glissez un agent pour le déplacer. Cliquez-en un, puis un autre, pour créer ou retirer une liaison.
        {link && <b style={{ color: "var(--accent)" }}> — liaison en cours, cliquez la cible.</b>}
      </div>
    </>
  );
}
