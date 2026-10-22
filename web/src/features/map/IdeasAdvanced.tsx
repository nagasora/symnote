import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { ArrowRight, Plus, Sprout } from "lucide-react";
import { apiFetch } from "../../api/client";
import { useGame } from "../game/GameContext";
import { Button } from "../../components/ui/Button";
import { Card } from "../../components/ui/Card";
import { EmptyState } from "../../components/ui/EmptyState";

interface IdeaNode {
  id: number;
  parent_id: number | null;
  title: string;
  linked_item_id: number | null;
  children?: IdeaNode[];
}
interface IdeasData {
  sessions: Array<{ id: number; title: string }>;
  active_session_id: number | null;
  tree: IdeaNode[];
}

export function IdeasAdvanced() {
  const [params] = useSearchParams();
  const { refresh, showToast } = useGame();
  const [data, setData] = useState<IdeasData | null>(null);
  const [title, setTitle] = useState("");
  const [childFor, setChildFor] = useState<number | null>(null);
  const [childTitle, setChildTitle] = useState("");
  const load = async (sessionId?: number) =>
    setData(
      await apiFetch<IdeasData>(
        `/api/ideas${sessionId ? `?session_id=${sessionId}` : ""}`,
      ),
    );
  useEffect(() => {
    void load();
  }, []);
  useEffect(() => {
    let active = true;
    const seedId = Number(params.get("seed"));
    if (seedId > 0)
      void apiFetch<{ seeds: Array<{ id: number; title: string }> }>(
        "/api/seeds",
      )
        .then((result) => {
          const seed = result.seeds.find((item) => item.id === seedId);
          if (active && seed) setTitle(seed.title);
        })
        .catch(() => {
          if (active) showToast("たねを読み込めませんでした。");
        });
    return () => {
      active = false;
    };
  }, [params, showToast]);
  const branch = (node: IdeaNode, depth = 0): React.ReactNode => (
    <div
      className="idea-branch"
      style={{ marginLeft: depth * 18 }}
      key={node.id}
    >
      <div>
        <span className="idea-node">
          <Sprout size={16} />
          {node.title}
        </span>
        {!node.linked_item_id && depth > 0 && (
          <Button
            size="sm"
            variant="quiet"
            onClick={async () => {
              await apiFetch(`/api/ideas/nodes/${node.id}/to-task`, {
                body: {},
              });
              await Promise.all([
                load(data?.active_session_id ?? undefined),
                refresh(),
              ]);
              showToast("きょうのクエストにしました");
            }}
          >
            <ArrowRight size={15} />
            クエストにする
          </Button>
        )}
        <Button size="sm" variant="quiet" onClick={() => setChildFor(node.id)}>
          <Plus size={15} />枝
        </Button>
      </div>
      {childFor === node.id && (
        <form
          onSubmit={async (event) => {
            event.preventDefault();
            if (!childTitle.trim() || !data?.active_session_id) return;
            await apiFetch(
              `/api/ideas/sessions/${data.active_session_id}/nodes`,
              { body: { parent_id: node.id, title: childTitle, kind: "idea" } },
            );
            setChildTitle("");
            setChildFor(null);
            await load(data.active_session_id);
          }}
        >
          <input
            autoFocus
            value={childTitle}
            onChange={(event) => setChildTitle(event.target.value)}
            placeholder="枝にする考え"
          />
          <Button size="sm" variant="sun">
            追加
          </Button>
        </form>
      )}
      {node.children?.map((child) => branch(child, depth + 1))}
    </div>
  );
  return (
    <div className="ideas-advanced">
      <Card>
        <h2>ミナとひろげる</h2>
        <p>たねを自由に枝分かれさせる上級者向けの思考マップです。</p>
        <form
          onSubmit={async (event) => {
            event.preventDefault();
            if (!title.trim()) return;
            await apiFetch("/api/ideas/sessions", { body: { title } });
            setTitle("");
            await load();
          }}
        >
          <input
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="新しいテーマ"
          />
          <Button variant="sun">
            <Plus size={17} />
            テーマを作る
          </Button>
        </form>
      </Card>
      {data?.sessions.length ? (
        <>
          <div className="idea-sessions">
            {data.sessions.map((session) => (
              <Button
                size="sm"
                variant={
                  data.active_session_id === session.id ? "sun" : "ghost"
                }
                key={session.id}
                onClick={() => void load(session.id)}
              >
                {session.title}
              </Button>
            ))}
          </div>
          <Card className="idea-tree">
            {data.tree.map((node) => branch(node))}
          </Card>
        </>
      ) : (
        <EmptyState title="まだ思考マップはありません">
          テーマを作ると、枝を自由に広げられます。
        </EmptyState>
      )}
    </div>
  );
}
