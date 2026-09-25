import { useMemo } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  Handle,
  Position,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { Task } from "../api/types";
import { agentColor, roleLabel, statusLabel } from "../lib/format";
import shared from "../styles/shared.module.css";
import styles from "./AgentGraph.module.css";
import { useI18n } from "../i18n";

type AgentNode = Node<
  { title: string; subtitle: string; status: string | null; color: string },
  "agent"
>;

function AgentTile({ data, selected }: NodeProps<AgentNode>) {
  const { t } = useI18n();
  return (
    <div
      className={`${styles.tile} ${selected ? styles.selectedTile : ""}`}
      data-status={data.status ?? undefined}
      style={{ "--agent-color": data.color } as React.CSSProperties}
    >
      <Handle type="target" position={Position.Left} className={styles.handle} />
      <span className={styles.tileAccent} />
      <span className={styles.tileContent}>
        <strong>{data.title}</strong>
        <small>
          {data.subtitle}
          {data.status && ` · ${statusLabel(data.status, t)}`}
        </small>
      </span>
      <span
        className={shared.statusDot}
        data-status={data.status ?? undefined}
        aria-hidden="true"
      />
      <Handle type="source" position={Position.Right} className={styles.handle} />
    </div>
  );
}

const nodeTypes = { agent: AgentTile };

interface AgentGraphProps {
  rootThreadId: string;
  tasks: Task[];
  rootStatus: string | null;
  selectedThreadId: string | null;
  onSelect: (threadId: string) => void;
}

export function AgentGraph({
  rootThreadId,
  rootStatus,
  tasks,
  selectedThreadId,
  onSelect,
}: AgentGraphProps) {
  const { t } = useI18n();
  const { nodes, edges } = useMemo(() => {
    const depth = new Map<string, number>([[rootThreadId, 0]]);
    const byParent = new Map<string, Task[]>();
    tasks.forEach((task) => {
      const parent = task.parent_thread_id ?? rootThreadId;
      byParent.set(parent, [...(byParent.get(parent) ?? []), task]);
    });
    const queue = [rootThreadId];
    while (queue.length) {
      const parent = queue.shift();
      if (!parent) break;
      for (const task of byParent.get(parent) ?? []) {
        if (depth.has(task.thread_id)) continue;
        depth.set(task.thread_id, (depth.get(parent) ?? 0) + 1);
        queue.push(task.thread_id);
      }
    }
    const levels = new Map<number, number>();
    const all: AgentNode[] = [
      {
        id: rootThreadId,
        type: "agent",
        position: { x: 0, y: Math.max(0, (tasks.length - 1) * 38) },
        data: {
          title: "SAYA",
          subtitle: t("主 Agent"),
          status: rootStatus,
          color: "var(--agent-saya)",
        },
        selected: rootThreadId === selectedThreadId,
      },
    ];
    const links: Edge[] = [];
    tasks.forEach((task) => {
      const level = depth.get(task.thread_id) ?? 1;
      const position = levels.get(level) ?? 0;
      levels.set(level, position + 1);
      all.push({
        id: task.thread_id,
        type: "agent",
        position: { x: level * 185, y: position * 74 },
        data: {
          title: task.title || roleLabel(task.role, t),
          subtitle: roleLabel(task.role, t),
          status: task.status,
          color: agentColor(task.thread_id),
        },
        selected: task.thread_id === selectedThreadId,
      });
      links.push({
        id: `${task.parent_thread_id ?? rootThreadId}-${task.thread_id}`,
        source: task.parent_thread_id ?? rootThreadId,
        target: task.thread_id,
        animated: task.status === "running",
        style: { stroke: "#a3a3a0", strokeWidth: 1.5 },
      });
    });
    return { nodes: all, edges: links };
  }, [rootThreadId, rootStatus, tasks, selectedThreadId, t]);

  return (
    <div className={styles.graph} aria-label={t("Agent 委派关系图")}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.18, maxZoom: 1 }}
        nodesDraggable={false}
        nodesConnectable={false}
        edgesFocusable={false}
        zoomOnScroll={false}
        panOnScroll={false}
        onNodeClick={(_, node) => onSelect(node.id)}
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={16} size={1} color="#dededb" />
        <Controls showInteractive={false} position="bottom-right" />
      </ReactFlow>
    </div>
  );
}
