"use client";

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { 
  Play, 
  FileText, 
  Download, 
  AlertTriangle, 
  CheckCircle2, 
  ShieldCheck, 
  Clock, 
  ArrowLeft,
  ChevronRight,
  ExternalLink,
  Sparkles
} from "lucide-react";
import { api } from "@/lib/api";
import { LectureItem } from "@/lib/types";
import { Breadcrumbs } from "@/components/navigation/Breadcrumbs";
import { VideoPlayer } from "@/components/media/VideoPlayer";
import { PDFViewer } from "@/components/media/PDFViewer";
import { Badge } from "@/components/ui/Badge";

export default function LectureViewPage() {
  const params = useParams();
  const router = useRouter();
  const lectureId = params.lectureId as string;

  const [lecture, setLecture] = useState<LectureItem | null>(null);
  const [videoAccess, setVideoAccess] = useState<{ video_id: string; title: string; duration: number } | null>(null);
  const [pdfAccess, setPdfAccess] = useState<{ download_url: string; file_name: string; page_count: number } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // PDF modal state
  const [isPdfModalOpen, setIsPdfModalOpen] = useState(false);

  useEffect(() => {
    async function loadLecture() {
      if (!lectureId) return;
      try {
        setLoading(true);
        const data = await api.getLecture(lectureId);
        setLecture(data);

        // Record student learning progress
        api.updateLearningProgress({ lecture_id: lectureId, playback_seconds: 0, completed: false }).catch(() => {});

        // If video exists, get video access embed
        if (data.has_video) {
          try {
            const vData = await api.getLectureAccess(lectureId);
            setVideoAccess(vData);
          } catch (vErr) {
            console.warn("Video access error:", vErr);
          }
        }

        // If PDF exists, verify PDF access
        if (data.has_pdf) {
          try {
            const pData = await api.getPdfAccess(lectureId);
            setPdfAccess(pData);
          } catch (pErr) {
            console.warn("PDF access error:", pErr);
          }
        }
      } catch (err: any) {
        setError(err.message || "Failed to load lecture");
      } finally {
        setLoading(false);
      }
    }
    loadLecture();
  }, [lectureId]);

  return (
    <div className="space-y-6">
      <Breadcrumbs
        items={[
          { label: "Curriculum", href: "/" },
          { label: lecture ? `#${lecture.lecture_index} - ${lecture.title}` : "Lecture" },
        ]}
      />

      {loading ? (
        <div className="space-y-4">
          <div className="w-full aspect-video rounded-3xl cw-card cw-skeleton" />
          <div className="h-20 cw-card cw-skeleton" />
        </div>
      ) : error ? (
        <div className="cw-card-elevated rounded-3xl p-12 text-center border border-rose-500/30 space-y-4">
          <AlertTriangle className="w-12 h-12 text-rose-400 mx-auto" />
          <h3 className="text-lg font-bold text-white">Lecture Unavailable</h3>
          <p className="text-sm text-cw-muted max-w-md mx-auto">{error}</p>
          <Link
            href="/"
            className="inline-flex items-center gap-2 px-5 py-2.5 bg-cw-primary hover:bg-cw-primary-hover text-white text-sm font-bold rounded-xl shadow-lg transition-all"
          >
            Back to Curriculum
          </Link>
        </div>
      ) : lecture ? (
        <div className="space-y-6">
          {/* Main Media Player / Content Area */}
          {lecture.has_video && videoAccess ? (
            /* STATE 1 & 2: VIDEO PLAYER AVAILABLE */
            <div className="space-y-4">
              <VideoPlayer
                videoId={videoAccess.youtube_video_id || videoAccess.video_id}
                playbackUrl={videoAccess.stream_url || videoAccess.playback_url || videoAccess.player_url || videoAccess.download_url}
                provider={videoAccess.storage_provider}
                title={lecture.title}
              />
            </div>
          ) : !lecture.has_video && lecture.has_pdf ? (
            /* STATE 3: PDF-ONLY LECTURE (Do not show broken / empty video player!) */
            <div className="cw-card-elevated rounded-3xl p-8 sm:p-12 border border-emerald-500/30 bg-gradient-to-tr from-emerald-950/30 via-cw-surface to-cw-surface text-center space-y-6">
              <div className="w-16 h-16 rounded-2xl bg-emerald-500/10 border border-emerald-500/30 flex items-center justify-center text-emerald-400 mx-auto shadow-lg">
                <FileText className="w-8 h-8" />
              </div>

              <div className="max-w-xl mx-auto space-y-2">
                <Badge variant="success">Synchronized Study Notes</Badge>
                <h2 className="text-2xl sm:text-3xl font-black text-white tracking-tight">
                  {lecture.title}
                </h2>
                <p className="text-xs sm:text-sm text-cw-muted leading-relaxed">
                  This lecture is provided as high-fidelity study notes, encrypted with Course Wallah digital protection.
                </p>
              </div>

              <div className="flex items-center justify-center gap-4 pt-2">
                <button
                  onClick={() => setIsPdfModalOpen(true)}
                  className="flex items-center gap-2 px-6 py-3 bg-emerald-600 hover:bg-emerald-500 text-white font-bold text-sm rounded-xl shadow-lg shadow-emerald-600/20 transition-all hover:scale-105"
                >
                  <FileText className="w-4 h-4" />
                  <span>Open PDF Notes</span>
                </button>
                {pdfAccess?.download_url && (
                  <a
                    href={pdfAccess.download_url}
                    target="_blank"
                    rel="noreferrer"
                    className="flex items-center gap-2 px-5 py-3 bg-cw-elevated hover:bg-cw-surface-hover text-white font-bold text-sm rounded-xl border border-cw-border transition-all"
                  >
                    <Download className="w-4 h-4" />
                    <span>Download PDF</span>
                  </a>
                )}
              </div>
            </div>
          ) : (
            /* STATE 4: NEITHER VIDEO NOR PDF */
            <div className="cw-card-elevated rounded-3xl p-12 text-center border border-cw-border space-y-4">
              <AlertTriangle className="w-12 h-12 text-amber-400 mx-auto" />
              <h3 className="text-lg font-bold text-white">Media Unavailable</h3>
              <p className="text-xs sm:text-sm text-cw-muted max-w-md mx-auto">
                No playable video stream or companion notes were provided by the source provider for this lecture index.
              </p>
            </div>
          )}

          {/* Lecture Metadata & Action Bar */}
          <div className="cw-card rounded-2xl p-6 border border-cw-border space-y-4">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
              <div className="space-y-2 flex-1 min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="font-mono text-xs font-bold text-cw-primary px-2.5 py-1 rounded-lg bg-cw-elevated border border-cw-border">
                    Lecture #{lecture.lecture_index}
                  </span>
                  {lecture.has_video && (
                    <Badge variant="primary">HD Video Stream</Badge>
                  )}
                  {lecture.has_pdf && (
                    <Badge variant="success">PDF Notes</Badge>
                  )}
                </div>
                <h1 className="text-xl sm:text-2xl font-bold text-white tracking-tight">
                  {lecture.title}
                </h1>
              </div>

              {/* Action Buttons */}
              <div className="flex items-center gap-3 flex-shrink-0">
                {lecture.has_pdf && (
                  <div className="flex items-center gap-2">
                    <button
                      onClick={() => setIsPdfModalOpen(true)}
                      className="flex items-center gap-2 px-4 py-2 bg-emerald-600/20 hover:bg-emerald-600/30 text-emerald-400 border border-emerald-500/30 text-xs sm:text-sm font-bold rounded-xl transition-all"
                    >
                      <FileText className="w-4 h-4" />
                      <span>View Notes</span>
                    </button>
                    {pdfAccess?.download_url && (
                      <a
                        href={pdfAccess.download_url}
                        target="_blank"
                        rel="noreferrer"
                        className="flex items-center gap-2 px-3 py-2 bg-cw-elevated hover:bg-cw-surface-hover text-white border border-cw-border text-xs sm:text-sm font-bold rounded-xl transition-all"
                      >
                        <Download className="w-4 h-4" />
                        <span>Download</span>
                      </a>
                    )}
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      ) : null}

      {/* PDF Viewer Modal */}
      {lecture && (
        <PDFViewer
          lectureId={lecture.id}
          title={lecture.title}
          isOpen={isPdfModalOpen}
          onClose={() => setIsPdfModalOpen(false)}
        />
      )}
    </div>
  );
}
