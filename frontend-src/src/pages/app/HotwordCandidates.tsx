import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { confirmCandidates, discardCandidates, editCandidate, fetchCandidates } from "@/api/endpoints";
import { Button } from "@/components/Button";
import { Diag } from "@/components/Diag";
import { EmptyState } from "@/components/EmptyState";
import { readApiError } from "@/api/client";

export function HotwordCandidates() {
  const qc = useQueryClient();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editWord, setEditWord] = useState("");
  const [editKind, setEditKind] = useState("");
  const [error, setError] = useState<string | null>(null);

  const candidates = useQuery({ queryKey: ["candidates"], queryFn: () => fetchCandidates() });

  const confirm = useMutation({
    mutationFn: (params: { ids: string[]; edits?: Record<string, { word?: string; kind?: string }> }) =>
      confirmCandidates(params.ids, params.edits),
    onSuccess: () => {
      setSelected(new Set());
      setEditingId(null);
      setError(null);  // 成功后清除错误
      qc.invalidateQueries({ queryKey: ["candidates"] });
    },
    onError: (e) => setError(readApiError(e)),
  });

  const discard = useMutation({
    mutationFn: () => discardCandidates([...selected]),
    onSuccess: () => {
      setSelected(new Set());
      setError(null);
      qc.invalidateQueries({ queryKey: ["candidates"] });
    },
    onError: (e) => setError(readApiError(e)),
  });

  const editMut = useMutation({
    mutationFn: (params: { id: string; word: string; kind: string }) =>
      editCandidate(params.id, params.word, params.kind),
    onSuccess: () => {
      // 编辑保存只更新内容，不改变选中状态、不确认进库
      setEditingId(null);
      setError(null);
      qc.invalidateQueries({ queryKey: ["candidates"] });
    },
    onError: (e) => setError(readApiError(e)),
  });

  function toggle(id: string) {
    setSelected((prev) => {
      const n = new Set(prev);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  }

  function startEdit(id: string, word: string, kind: string) {
    setEditingId(id);
    setEditWord(word);
    setEditKind(kind);
  }

  function confirmWithEdit() {
    if (!editingId) return;
    // 编辑保存：只更新写法/分类，不确认进库、不清空选中。
    // 用户改完写法后，可以用顶部的"确认选中"批量确认（含这个已编辑的词）。
    editMut.mutate({ id: editingId, word: editWord.trim(), kind: editKind.trim() });
  }

  const items = candidates.data ?? [];
  // allSelected 用 every 检查，而非 selected.size === items.length。
  // 后者在 selected 含已删除的 stale id 时会误判（size 对不上但可见项全选了）。
  const allSelected = items.length > 0 && items.every((i) => selected.has(i.id));

  return (
    <div>
      {error && <Diag code="CAND_E">{error}</Diag>}
      <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "center", marginBottom: "var(--space-4)" }}>
        <Button variant="ghost" size="sm" onClick={() => setSelected(allSelected ? new Set() : new Set(items.map((i) => i.id)))}>
          {allSelected ? "取消全选" : "全选"}
        </Button>
        <Button variant="primary" size="sm" disabled={selected.size === 0} loading={confirm.isPending} onClick={() => { if (window.confirm(`确认将 ${selected.size} 个词加入正式热词库？加入后会参与后续转写纠错。`)) confirm.mutate({ ids: [...selected] }); }}>
          确认选中 ({selected.size})
        </Button>
        <Button variant="danger" size="sm" disabled={selected.size === 0} loading={discard.isPending} onClick={() => { if (window.confirm(`确定丢弃 ${selected.size} 个候选词？丢弃后不再推荐。`)) discard.mutate(); }}>
          丢弃选中
        </Button>
        <span className="meta" style={{ marginLeft: "auto", fontSize: "var(--text-xs)", color: "var(--fg-subtle)" }}>
          共 {items.length} 个待确认
        </span>
      </div>

      {items.length === 0 ? (
        <EmptyState title="暂无候选词" description="转写并生成纪要后，大模型会自动发现候选热词。" />
      ) : (
        items.map((c) => (
          <div key={c.id} style={{ display: "flex", gap: "var(--space-3)", padding: "var(--space-3)", borderBottom: "1px solid var(--border-default)", alignItems: "flex-start" }}>
            <input type="checkbox" checked={selected.has(c.id)} onChange={() => toggle(c.id)} style={{ marginTop: 4 }} />
            <div style={{ flex: 1 }}>
              {editingId === c.id ? (
                <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "center" }}>
                  <input className="field" value={editWord} onChange={(e) => setEditWord(e.target.value)} style={{ width: 140 }} />
                  <input className="field" value={editKind} onChange={(e) => setEditKind(e.target.value)} style={{ width: 100 }} />
                  <Button size="sm" variant="primary" loading={editMut.isPending} onClick={confirmWithEdit}>保存</Button>
                  <Button size="sm" variant="ghost" onClick={() => setEditingId(null)}>取消</Button>
                </div>
              ) : (
                <>
                  <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "center" }}>
                    <strong>{c.word}</strong>
                    <span className="tag">{c.kind}</span>
                    <span className="meta" style={{ fontSize: "var(--text-xs)" }}>频次×{c.frequency}</span>
                    {c.confidence < 0.6 && <span style={{ color: "#d97706", fontSize: "var(--text-xs)" }}>⚠️ 疑似</span>}
                    <span className="meta" style={{ fontSize: "var(--text-xs)", color: "var(--fg-subtle)" }}>{c.confidence.toFixed(2)}</span>
                  </div>
                  {c.example && (
                    <div className="meta" style={{ fontSize: "var(--text-xs)", color: "var(--fg-muted)", marginTop: 2 }}>
                      例：{c.example}
                    </div>
                  )}
                  <button
                    onClick={() => startEdit(c.id, c.word, c.kind)}
                    style={{ marginTop: 4, padding: 0, background: "none", border: "none", color: "var(--fg-subtle)", cursor: "pointer", fontSize: "var(--text-xs)" }}
                  >
                    ✎ 编辑
                  </button>
                </>
              )}
            </div>
          </div>
        ))
      )}
    </div>
  );
}
