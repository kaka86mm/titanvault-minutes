import { api, getStoredToken } from "./client";
import type {
  Hotword,
  HotwordCandidate,
  HotwordStatus,
  LoginResponse,
  Recording,
  RecordingDetail,
  RecordingTask,
  SpeakerCandidate,
  Settings,
  SystemStatus,
  User,
  Voiceprint,
} from "./types";

// Thin one-call-per-export module. The TanStack Query hooks live alongside
// pages so they can shape the data when needed, but the verbs themselves
// are kept here so it's easy to grep for "which endpoints does the UI hit".

// -------- auth (single-user desktop build: login is unused but kept harmless) --------

export async function login(username: string, password: string): Promise<LoginResponse> {
  const { data } = await api.post<LoginResponse>("/auth/login", { username, password });
  return data;
}

export async function logout(): Promise<void> {
  await api.post("/auth/logout");
}

export async function changePassword(currentPassword: string, newPassword: string): Promise<User> {
  const { data } = await api.post<User>("/auth/change-password", {
    current_password: currentPassword,
    new_password: newPassword,
  });
  return data;
}

export async function fetchMe(): Promise<User> {
  const { data } = await api.get<User>("/me");
  return data;
}

// -------- settings (OpenAI-compatible LLM endpoint) --------

export async function fetchSettings(): Promise<Settings> {
  const { data } = await api.get<Settings>("/settings");
  return data;
}

export async function patchSettings(payload: Partial<{
  llm_api_key: string;
  llm_api_base: string;
  llm_model: string;
}>): Promise<Settings> {
  const { data } = await api.patch<Settings>("/settings", payload);
  return data;
}

// -------- recordings --------

export async function fetchRecordings(params: {
  scope?: "mine" | "team";
  q?: string;
  meeting_type?: string;
}): Promise<Recording[]> {
  const { data } = await api.get<Recording[]>("/recordings", { params });
  return data;
}

export async function fetchRecording(id: string): Promise<RecordingDetail> {
  const { data } = await api.get<RecordingDetail>(`/recordings/${id}`);
  return data;
}

export async function uploadRecording(input: {
  file: File;
  title: string;
  meeting_type?: string;
  tag?: string;
  auto_process?: boolean;
  expected_speakers?: number | null;
  onProgress?: (pct: number) => void;
}): Promise<Recording> {
  const form = new FormData();
  form.append("file", input.file);
  form.append("title", input.title);
  if (input.meeting_type) form.append("meeting_type", input.meeting_type);
  if (input.tag) form.append("tag", input.tag);
  form.append("auto_process", String(input.auto_process ?? true));
  if (input.expected_speakers && input.expected_speakers >= 2) {
    form.append("expected_speakers", String(input.expected_speakers));
  }
  const { data } = await api.post<Recording>("/recordings", form, {
    onUploadProgress: (e) => {
      if (input.onProgress && e.total) {
        input.onProgress(Math.round((e.loaded / e.total) * 100));
      }
    },
  });
  return data;
}

export async function processRecording(id: string): Promise<{ recording_id: string; status: string }> {
  const { data } = await api.post(`/recordings/${id}/process`);
  return data;
}

export async function transcribeRecording(id: string): Promise<{ recording_id: string; segments: number; speakers: number }> {
  const { data } = await api.post(`/recordings/${id}/transcribe`);
  return data;
}

export async function summarizeRecording(id: string): Promise<{ recording_id: string; model: string; summary_id: string; version: number }> {
  const { data } = await api.post(`/recordings/${id}/summarize`);
  return data;
}

export async function reviseSummary(id: string, instruction: string): Promise<{ recording_id: string; model: string; summary_id: string; version: number }> {
  const { data } = await api.post(`/recordings/${id}/summary/revise`, { instruction });
  return data;
}

export async function generateEmotion(id: string): Promise<{ status: string; recording_id: string }> {
  const { data } = await api.post(`/recordings/${id}/emotion`);
  return data;
}

export async function fetchSpeakerCandidates(id: string): Promise<SpeakerCandidate[]> {
  const { data } = await api.get<SpeakerCandidate[]>(`/recordings/${id}/speaker-candidates`);
  return data;
}

export async function mergeSpeakers(recordingId: string, from: string, into: string): Promise<{ moved_segments: number; into: string }> {
  const { data } = await api.post(`/recordings/${recordingId}/speakers/merge`, { from, into });
  return data;
}

export async function renameSpeaker(recordingId: string, speaker: string, payload: {
  name: string;
  voiceprint_id?: string;
}): Promise<{ recording_id: string; speaker: string; name: string; updated_segments: number }> {
  const { data } = await api.patch(`/recordings/${recordingId}/speakers/${speaker}`, payload);
  return data;
}

// -------- tasks --------

export async function fetchTasks(): Promise<RecordingTask[]> {
  const { data } = await api.get<RecordingTask[]>("/tasks");
  return data;
}

// -------- hotwords (manual maintenance) --------

export async function fetchHotwords(params: {
  state?: string;
  q?: string;
  protected?: "0" | "1";
}): Promise<Hotword[]> {
  const { data } = await api.get<Hotword[]>("/hotwords", { params });
  return data;
}

export async function fetchHotwordStatus(): Promise<HotwordStatus> {
  const { data } = await api.get<HotwordStatus>("/hotwords/status");
  return data;
}

export async function createHotword(payload: {
  word: string;
  kind?: string;
  aliases?: string;
  scope?: string;
  weight?: number;
  protected?: boolean;
}): Promise<Hotword> {
  const { data } = await api.post<Hotword>("/hotwords", payload);
  return data;
}

export async function patchHotword(id: string, payload: Partial<Pick<Hotword, "kind" | "aliases" | "scope" | "weight" | "active" | "state" | "protected">>): Promise<Hotword> {
  const { data } = await api.patch<Hotword>(`/hotwords/${id}`, payload);
  return data;
}

export async function deleteHotword(id: string): Promise<{ ok: boolean; id: string }> {
  const { data } = await api.delete(`/hotwords/${id}`);
  return data;
}

export async function maintainHotwords(): Promise<{ expired: number; rescored: number; status: HotwordStatus }> {
  const { data } = await api.post("/hotwords/maintain");
  return data;
}

export async function importHotwordsTxt(file: File): Promise<{ inserted: number; skipped: number; total: number }> {
  const form = new FormData();
  form.append("file", file);
  const { data } = await api.post("/hotwords/import", form);
  return data;
}

// -------- voiceprints --------

export async function fetchVoiceprints(): Promise<Voiceprint[]> {
  const { data } = await api.get<Voiceprint[]>("/voiceprints");
  return data;
}

export async function createVoiceprint(input: {
  name: string;
  threshold?: number;
  scope?: "personal" | "team" | "global";
  team_id?: string;
  file: File;
}): Promise<Voiceprint> {
  const form = new FormData();
  form.append("name", input.name);
  if (input.threshold != null) form.append("threshold", String(input.threshold));
  if (input.scope) form.append("scope", input.scope);
  if (input.team_id) form.append("team_id", input.team_id);
  form.append("file", input.file);
  const { data } = await api.post<Voiceprint>("/voiceprints", form);
  return data;
}

export async function createVoiceprintFromRecording(payload: {
  recording_id: string;
  speaker: string;
  name?: string;
  segment_ids?: string[];
  profile_id?: string;
  update_current_recording?: boolean;
  threshold?: number;
  scope?: "personal" | "team" | "global";
  team_id?: string;
}): Promise<{
  profile: Voiceprint;
  sample_count: number;
  sample_duration: number;
  sample_duration_label: string;
  updated_segments: number;
}> {
  const { data } = await api.post("/voiceprints/from-recording", payload);
  return data;
}

export async function patchVoiceprint(id: string, payload: Partial<Pick<Voiceprint, "name" | "threshold" | "active">>): Promise<Voiceprint> {
  const { data } = await api.patch<Voiceprint>(`/voiceprints/${id}`, payload);
  return data;
}

// -------- system --------

export async function fetchSystemStatus(): Promise<SystemStatus> {
  const { data } = await api.get<SystemStatus>("/system/status");
  return data;
}

// Helpers that don't go through axios (audio src, download hrefs). They need
// the same origin resolution as the axios client so a 5173-served frontend
// points its <audio> src at :8000.
function apiPrefix(): string {
  if (typeof window === "undefined") return "/api";
  const { protocol, hostname, port } = window.location;
  if (port === "5174") return "/api";
  if (port === "5173" || port === "") return `${protocol}//${hostname}:8000/api`;
  return "/api";
}

// Append the session token as a query param. Media/download URLs are loaded
// directly by the browser (<audio src>, <a download>), which can't attach the
// Authorization header — so the backend also accepts ?token=.
function withToken(url: string): string {
  const token = getStoredToken();
  if (!token) return url;
  return `${url}${url.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}`;
}

export function recordingAudioUrl(id: string): string {
  return withToken(`${apiPrefix()}/recordings/${id}/audio`);
}

export function exportTranscriptUrl(id: string, fmt: "md" | "docx" = "md"): string {
  return withToken(`${apiPrefix()}/recordings/${id}/export/transcript.md${fmt === "docx" ? "?format=docx" : ""}`);
}

export function exportSummaryUrl(id: string, fmt: "md" | "docx" = "md"): string {
  return withToken(`${apiPrefix()}/recordings/${id}/export/summary.md${fmt === "docx" ? "?format=docx" : ""}`);
}

export function exportSummaryVersionUrl(id: string, summaryId: string, fmt: "md" | "docx" = "md"): string {
  return withToken(`${apiPrefix()}/recordings/${id}/export/summaries/${summaryId}.md${fmt === "docx" ? "?format=docx" : ""}`);
}

export function exportEmotionUrl(id: string, fmt: "md" | "docx" = "md"): string {
  return withToken(`${apiPrefix()}/recordings/${id}/export/emotion.md${fmt === "docx" ? "?format=docx" : ""}`);
}

export function segmentAudioUrl(recordingId: string, segmentId: string): string {
  return withToken(`${apiPrefix()}/recordings/${recordingId}/segments/${segmentId}/audio`);
}

// -------- hotword candidates --------

export async function fetchCandidates(sort?: string): Promise<HotwordCandidate[]> {
  const { data } = await api.get<HotwordCandidate[]>("/hotwords/candidates", { params: sort ? { sort } : {} });
  return data;
}

export async function confirmCandidates(
  ids: string[],
  edits?: Record<string, { word?: string; kind?: string }>,
): Promise<{ confirmed: number }> {
  const { data } = await api.post("/hotwords/candidates/confirm", { ids, edits: edits || {} });
  return data;
}

export async function editCandidate(
  candidateId: string,
  word: string,
  kind: string,
): Promise<{ id: string; word: string; kind: string }> {
  const { data } = await api.patch(`/hotwords/candidates/${candidateId}`, { word, kind });
  return data;
}

export async function discardCandidates(ids: string[]): Promise<{ discarded: number }> {
  const { data } = await api.post("/hotwords/candidates/discard", { ids });
  return data;
}
