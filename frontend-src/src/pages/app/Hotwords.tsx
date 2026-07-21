import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { HotwordCandidates } from "./HotwordCandidates";
import {
  createHotword,
  deleteHotword,
  fetchHotwordStatus,
  fetchHotwords,
  importHotwordsTxt,
  maintainHotwords,
  patchHotword,
} from "@/api/endpoints";
import { PageHead } from "@/components/PageHead";
import { Button } from "@/components/Button";
import { Status } from "@/components/Status";
import { Icon } from "@/components/Icon";
import { Diag } from "@/components/Diag";
import { EmptyState } from "@/components/EmptyState";
import { readApiError } from "@/api/client";
import { formatRelative } from "@/utils/format";

const STATES = ["全部", "active", "discarded", "expired", "protected"] as const;

export function Hotwords() {
  const qc = useQueryClient();

  const [state, setState] = useState<string>("全部");
  const [q, setQ] = useState("");
  const [protectedOnly, setProtectedOnly] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [tab, setTab] = useState<"active" | "candidates">("active");
  const fileInputRef = useRef<HTMLInputElement>(null);

  // 新增热词表单
  const [newWord, setNewWord] = useState("");
  const [newKind, setNewKind] = useState("");
  const [newAliases, setNewAliases] = useState("");
  const [newWeight, setNewWeight] = useState(8);
  const [newProtected, setNewProtected] = useState(true);

  const status = useQuery({ queryKey: ["hotwords-status"], queryFn: fetchHotwordStatus });
  const rows = useQuery({
    queryKey: ["hotwords", state, q, protectedOnly],
    queryFn: () =>
      fetchHotwords({
        state: state === "全部" ? "" : state,
        q,
        protected: protectedOnly ? "1" : undefined,
      }),
  });

  function invalidate() {
    qc.invalidateQueries({ queryKey: ["hotwords"] });
    qc.invalidateQueries({ queryKey: ["hotwords-status"] });
  }

  const create = useMutation({
    mutationFn: () =>
      createHotword({
        word: newWord.trim(),
        kind: newKind.trim() || "术语",
        aliases: newAliases.trim(),
        weight: newWeight,
        protected: newProtected,
      }),
    onSuccess: () => {
      setInfo(`已添加热词「${newWord.trim()}」。`);
      setError(null);
      setNewWord("");
      setNewAliases("");
      invalidate();
    },
    onError: (err) => setError(readApiError(err)),
  });

  const togglePatch = useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: Parameters<typeof patchHotword>[1] }) =>
      patchHotword(id, payload),
    onSuccess: invalidate,
    onError: (err) => setError(readApiError(err)),
  });

  const remove = useMutation({
    mutationFn: (id: string) => deleteHotword(id),
    onSuccess: () => {
      setInfo("已删除热词。");
      invalidate();
    },
    onError: (err) => setError(readApiError(err)),
  });

  const maintain = useMutation({
    mutationFn: maintainHotwords,
    onSuccess: (data) => {
      setInfo(`已重算评分。过期 ${data.expired}，更新 ${data.rescored}。`);
      invalidate();
    },
    onError: (err) => setError(readApiError(err)),
  });

  const importTxt = useMutation({
    mutationFn: (file: File) => importHotwordsTxt(file),
    onSuccess: (r) => {
      setError(null);
      setInfo(`导入完成：新增 ${r.inserted}，跳过 ${r.skipped}（文件共 ${r.total} 条）。`);
      invalidate();
    },
    onError: (err) => setError(readApiError(err)),
  });

  function onPickFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = ""; // reset so re-picking the same file re-fires onChange
    if (!file) return;
    setError(null);
    setInfo(null);
    importTxt.mutate(file);
  }

  function submitCreate() {
    setError(null);
    setInfo(null);
    if (!newWord.trim()) {
      setError("请填写要添加的词。");
      return;
    }
    create.mutate();
  }

  return (
    <div className="page-body page--object-index">
      <PageHead
        title="热词"
        subtitle={
          status.data
            ? `共 ${status.data.total} 条 · 启用 ${status.data.active} · 受保护 ${status.data.protected} · 已过期 ${status.data.expired}`
            : "加载中…"
        }
      />

      {error && <Diag code="HW_E_ACTION">{error}</Diag>}
      {info && <Diag code="HW_OK" tone="info">{info}</Diag>}

      {/* tab 切换 */}
      <div style={{ display: "flex", gap: "var(--space-1)", marginBottom: "var(--space-4)", borderBottom: "1px solid var(--border-default)" }}>
        <button
          onClick={() => setTab("active")}
          style={{
            padding: "var(--space-2) var(--space-4)", background: "none", border: "none", cursor: "pointer",
            fontWeight: tab === "active" ? "var(--weight-semibold)" : "var(--weight-regular)",
            color: tab === "active" ? "var(--fg-default)" : "var(--fg-subtle)",
            borderBottom: tab === "active" ? "2px solid var(--accent-default)" : "2px solid transparent",
          }}
        >
          正式热词
        </button>
        <button
          onClick={() => setTab("candidates")}
          style={{
            padding: "var(--space-2) var(--space-4)", background: "none", border: "none", cursor: "pointer",
            fontWeight: tab === "candidates" ? "var(--weight-semibold)" : "var(--weight-regular)",
            color: tab === "candidates" ? "var(--fg-default)" : "var(--fg-subtle)",
            borderBottom: tab === "candidates" ? "2px solid var(--accent-default)" : "2px solid transparent",
          }}
        >
          候选审阅
        </button>
      </div>

      {tab === "candidates" && <HotwordCandidates />}

      {tab === "active" && (
      <>
      {/* 新增热词 */}
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: "var(--space-2)",
          alignItems: "flex-end",
          marginBottom: "var(--space-4)",
          padding: "var(--space-3) var(--space-4)",
          border: "1px solid var(--border-default)",
          borderRadius: "var(--radius-md)",
          background: "var(--bg-surface)",
        }}
      >
        <label style={{ display: "flex", flexDirection: "column", gap: "var(--space-1)" }}>
          <span className="meta" style={{ fontSize: "var(--text-2xs)" }}>词</span>
          <input
            className="field"
            style={{ width: 180, height: 32 }}
            placeholder="如：帕萨思"
            value={newWord}
            onChange={(e) => setNewWord(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") submitCreate(); }}
          />
        </label>
        <label style={{ display: "flex", flexDirection: "column", gap: "var(--space-1)" }}>
          <span className="meta" style={{ fontSize: "var(--text-2xs)" }}>类型</span>
          <input
            className="field"
            style={{ width: 120, height: 32 }}
            placeholder="客户/产品/术语"
            value={newKind}
            onChange={(e) => setNewKind(e.target.value)}
          />
        </label>
        <label style={{ display: "flex", flexDirection: "column", gap: "var(--space-1)" }}>
          <span className="meta" style={{ fontSize: "var(--text-2xs)" }}>别名（逗号分隔）</span>
          <input
            className="field"
            style={{ width: 200, height: 32 }}
            placeholder="可选，如：帕萨,passas"
            value={newAliases}
            onChange={(e) => setNewAliases(e.target.value)}
          />
        </label>
        <label style={{ display: "flex", flexDirection: "column", gap: "var(--space-1)" }}>
          <span className="meta" style={{ fontSize: "var(--text-2xs)" }}>权重 1-10</span>
          <input
            className="field"
            type="number"
            min={1}
            max={10}
            style={{ width: 80, height: 32 }}
            value={newWeight}
            onChange={(e) => setNewWeight(Math.max(1, Math.min(10, Number(e.target.value) || 1)))}
          />
        </label>
        <label style={{ display: "inline-flex", gap: "var(--space-2)", alignItems: "center", height: 32 }}>
          <input type="checkbox" checked={newProtected} onChange={(e) => setNewProtected(e.target.checked)} />
          <span className="meta">受保护（不过期）</span>
        </label>
        <Button variant="primary" size="sm" loading={create.isPending} onClick={submitCreate}>
          <Icon name="plus" size={14} /> 添加
        </Button>
        <Button
          variant="secondary"
          size="sm"
          loading={importTxt.isPending}
          onClick={() => fileInputRef.current?.click()}
        >
          <Icon name="upload" size={14} /> 导入 txt
        </Button>
        <input
          ref={fileInputRef}
          type="file"
          accept=".txt,text/plain"
          style={{ display: "none" }}
          onChange={onPickFile}
        />
        <Button
          variant="ghost"
          size="sm"
          loading={maintain.isPending}
          onClick={() => { setError(null); setInfo(null); maintain.mutate(); }}
          style={{ marginLeft: "auto" }}
        >
          重算评分
        </Button>
      </div>

      <div className="filter-bar">
        <div className="filter-bar__search">
          <Icon name="search" size={14} className="filter-bar__search-icon" />
          <input
            type="search"
            placeholder="搜索词、别名、来源、类别"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>
        <div className="filter-bar__segments" role="tablist" aria-label="状态">
          {STATES.map((s) => (
            <button key={s} role="tab" aria-selected={state === s} onClick={() => setState(s)}>
              {s === "active" ? "启用中" : s === "expired" ? "已过期" : s === "protected" ? "保护" : "全部"}
            </button>
          ))}
        </div>
        <label style={{ display: "inline-flex", gap: "var(--space-2)", alignItems: "center" }}>
          <input type="checkbox" checked={protectedOnly} onChange={(e) => setProtectedOnly(e.target.checked)} />
          <span className="meta">仅看受保护</span>
        </label>
      </div>

      {rows.isError && <Diag code="HW_E_LIST">{readApiError(rows.error)}</Diag>}
      {rows.data && rows.data.length === 0 && !rows.isLoading && (
        <EmptyState description="当前筛选下没有任何热词。点上方「添加」手动维护。" />
      )}

      {rows.data && rows.data.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th className="col-grow">词</th>
              <th className="col-fit">类型</th>
              <th className="col-fit">别名</th>
              <th className="col-fit">来源</th>
              <th className="col-fit">权重</th>
              <th className="col-fit">命中</th>
              <th className="col-fit">状态</th>
              <th className="col-fit">最近</th>
              <th className="col-fit">操作</th>
            </tr>
          </thead>
          <tbody>
            {rows.data.map((row) => (
              <tr key={row.id}>
                <td>
                  <div className="cell-stack">
                    <span className="primary">{row.word}</span>
                    {row.protected && <span className="meta" style={{ fontSize: "var(--text-2xs)" }}>受保护</span>}
                  </div>
                </td>
                <td className="cat">{row.kind}</td>
                <td className="meta" style={{ maxWidth: 240, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {row.aliases || "—"}
                </td>
                <td className="meta">{row.source}</td>
                <td className="meta">{row.weight}</td>
                <td className="meta">{row.hit_count}</td>
                <td>
                  <Status
                    tone={
                      row.state === "expired"
                        ? "muted"
                        : row.protected
                          ? "accent"
                          : row.active
                            ? "moss"
                            : "rust"
                    }
                  >
                    {row.state === "expired" ? "已过期" : row.active ? (row.protected ? "保护" : "启用") : "停用"}
                  </Status>
                </td>
                <td className="meta">{formatRelative(row.last_seen_at)}</td>
                <td>
                  <div style={{ display: "flex", gap: "var(--space-1)" }}>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() =>
                        togglePatch.mutate({ id: row.id, payload: { protected: !row.protected } })
                      }
                    >
                      {row.protected ? "取消保护" : "标为保护"}
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() =>
                        togglePatch.mutate({
                          id: row.id,
                          payload: {
                            state: row.state === "discarded" ? "active" : "discarded",
                            active: row.state === "discarded" ? true : false,
                          },
                        })
                      }
                    >
                      {row.state === "discarded" ? "启用" : "停用"}
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => {
                        if (confirm(`删除热词「${row.word}」？`)) remove.mutate(row.id);
                      }}
                    >
                      <Icon name="trash-2" size={14} />
                    </Button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {rows.data && rows.data.length > 0 && (
        <div className="list-footer">
          <span className="count">共 {rows.data.length} 条（最多展示 1000 条）</span>
        </div>
      )}
      </>
      )}
    </div>
  );
}
