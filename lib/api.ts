import { 
  AppItem, 
  BatchItem, 
  LectureItem, 
  JobItem, 
  YouTubeDiagnostics,
  StudentUser,
  StudentActivityItem,
  AIResponse
} from "./types";

// Keep browser requests same-origin; vercel.json forwards /api/* to the live FastAPI backend.
const API_BASE = "/api/v1";

export function getStudentToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("cw_student_token") || null;
}

export function setStudentToken(token: string) {
  if (typeof window !== "undefined") {
    localStorage.setItem("cw_student_token", token);
  }
}

export function clearStudentToken() {
  if (typeof window !== "undefined") {
    localStorage.removeItem("cw_student_token");
  }
}

export function getAdminToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("cw_admin_token") || null;
}

export function setAdminToken(token: string) {
  if (typeof window !== "undefined") {
    localStorage.setItem("cw_admin_token", token);
  }
}

export function clearAdminToken() {
  if (typeof window !== "undefined") {
    localStorage.removeItem("cw_admin_token");
  }
}

async function request<T>(endpoint: string, options: RequestInit = {}): Promise<T> {
  const studentToken = getStudentToken();
  const adminToken = getAdminToken();
  const token = studentToken || adminToken;

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options.headers as Record<string, string> || {}),
  };

  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  const res = await fetch(`${API_BASE}${endpoint}`, {
    ...options,
    headers,
  });

  if (!res.ok) {
    let errorDetail = `Request failed: HTTP ${res.status}`;
    try {
      const errJson = await res.json();
      errorDetail = errJson.detail || errJson.message || errorDetail;
    } catch {
      // fallback
    }
    throw new Error(errorDetail);
  }

  return res.json();
}

export const api = {
  // Public catalog routes
  getApps: () => request<AppItem[]>("/apps"),
  getAppBySlug: (slug: string) => request<AppItem>(`/apps/${slug}`),
  getAppBatches: (slug: string) => request<BatchItem[]>(`/apps/${slug}/batches`),
  getBatchHierarchy: (slug: string) => request<BatchItem>(`/batches/${slug}`),
  getLecture: (id: string) => request<LectureItem>(`/lectures/${id}`),
  getLectureAccess: (id: string) => request<{ video_id: string; embed_url: string; title: string; duration: number }>(`/lectures/${id}/access`),
  getPdfAccess: (id: string) => request<{ download_url: string; access_url?: string; file_name: string; page_count: number }>(`/pdfs/${id}/access`),
  search: (query: string) => request<{ query: string; results_count: number; apps: AppItem[]; batches: BatchItem[]; lectures: LectureItem[] }>(`/search?q=${encodeURIComponent(query)}`),

  // Student Authentication & Profile routes
  studentRegister: async (data: { name: string; email: string; password: string; confirm_password: string }) => {
    const res = await request<{ status: string; token: string; student: StudentUser }>("/auth/register", {
      method: "POST",
      body: JSON.stringify(data),
    });
    if (res.token) setStudentToken(res.token);
    return res;
  },

  studentLogin: async (data: { email: string; password: string }) => {
    const res = await request<{ status: string; token: string; student: StudentUser }>("/auth/login", {
      method: "POST",
      body: JSON.stringify(data),
    });
    if (res.token) setStudentToken(res.token);
    return res;
  },

  getStudentProfile: () => request<StudentUser>("/auth/me"),
  
  studentLogout: async () => {
    try {
      await request<{ status: string }>("/auth/logout", { method: "POST" });
    } finally {
      clearStudentToken();
    }
  },

  changePassword: (data: { old_password: string; new_password: string }) =>
    request<{ status: string; message: string }>("/auth/change-password", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  getMyBatches: () => request<BatchItem[]>("/auth/my-batches"),
  
  enrollInBatch: (batchIdentifier: string) =>
    request<{ status: string; message: string; batch_slug: string }>(`/auth/enroll/${batchIdentifier}`, {
      method: "POST",
    }),

  updateLearningProgress: (data: { lecture_id: string; playback_seconds?: number; completed?: boolean }) =>
    request<{ status: string }>("/auth/progress", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  getLearningProgress: () => request<StudentActivityItem[]>("/auth/progress"),

  // Student AI Assistant
  queryAI: (message: string) =>
    request<AIResponse>("/ai/query", {
      method: "POST",
      body: JSON.stringify({ message }),
    }),

  // Student Contact & Inquiries
  submitContact: (data: { name: string; email: string; subject: string; message: string }) =>
    request<{ status: string; message: string }>("/contact", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  // Protected Admin routes
  adminLogin: (password: string, username = "admin") =>
    request<{ status: string; token: string; username: string }>("/admin/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  getAdminStats: () => request<{
    apps: number;
    batches: number;
    subjects: number;
    lectures: number;
    published_lectures: number;
    videos: number;
    pdfs: number;
    active_jobs: number;
    failed_jobs: number;
  }>("/admin/stats"),
  getAdminApps: () => request<AppItem[]>("/admin/apps"),
  createApp: (data: { name: string; description?: string; icon_url?: string }) =>
    request<{ status: string; app: AppItem }>("/admin/apps", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  getAdminBatches: () => request<BatchItem[]>("/admin/batches"),
  getAdminJobs: () => request<JobItem[]>("/admin/jobs"),
  retryJob: (jobId: string) =>
    request<{ status: string; message: string }>(`/admin/jobs/${jobId}/retry`, {
      method: "POST",
    }),
  togglePublishLecture: (lectureId: string) =>
    request<{ lecture_id: string; new_status: string }>(`/admin/lectures/${lectureId}/toggle-publish`, {
      method: "POST",
    }),
  getYouTubeDiagnostics: () => request<YouTubeDiagnostics>("/admin/youtube"),
  refreshYouTubeDiagnostics: () =>
    request<{ status: string; channel: YouTubeDiagnostics["channel"] }>("/admin/youtube/refresh", {
      method: "POST",
    }),
  resumeBatchQueue: (batchId = "all") =>
    request<{ status: string; resumed_controllers: string[]; message: string }>(`/admin/youtube/resume/${batchId}`, {
      method: "POST",
    }),
  getWatermarkProfile: () => request<{
    id: string;
    name: string;
    text: string;
    opacity: number;
    animation_mode: string;
    movement_interval: number;
    crf: number;
    enabled: boolean;
  }>("/admin/watermark"),
  updateWatermarkProfile: (data: any) =>
    request<{ status: string; message: string }>("/admin/watermark", {
      method: "PUT",
      body: JSON.stringify(data),
    }),
};
