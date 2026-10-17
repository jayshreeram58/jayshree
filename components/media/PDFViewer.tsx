"use client";

import { useEffect, useState, useRef } from "react";
import { X, FileText, Loader2, AlertCircle, ShieldCheck, Lock, RefreshCw, BookOpen } from "lucide-react";
import { api } from "@/lib/api";

interface PDFViewerProps {
  lectureId: string;
  title: string;
  isOpen?: boolean;
  onClose?: () => void;
  inline?: boolean;
}

export function PDFViewer({
  lectureId,
  title,
  isOpen = true,
  onClose,
  inline = false,
}: PDFViewerProps) {
  const [downloadUrl, setDownloadUrl] = useState<string | null>(null);
  const [pageCount, setPageCount] = useState<number>(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastRefreshed, setLastRefreshed] = useState<string>("");

  // Function to load or silently refresh the authorized PDF access URL
  const loadPdfAccess = async (isInitial = false) => {
    if (isInitial) setLoading(true);
    setError(null);
    try {
      const data = await api.getPdfAccess(lectureId);
      setDownloadUrl(data.download_url || data.access_url || null);
      setPageCount(data.page_count || 0);
      setLastRefreshed(new Date().toLocaleTimeString());
    } catch (err: any) {
      if (isInitial) {
        setError(err.message || "Failed to generate authorized PDF access link");
      }
    } finally {
      if (isInitial) setLoading(false);
    }
  };

  useEffect(() => {
    if (!lectureId || (!isOpen && !inline)) return;

    let isMounted = true;
    loadPdfAccess(true);

    // SILENT AUTO-RENEWAL LOOP:
    // Automatically re-authorizes the session every 10 minutes (600,000 ms)
    // so the student can study for hours continuously without any timeout or interruption.
    const refreshInterval = setInterval(() => {
      if (isMounted) {
        loadPdfAccess(false);
      }
    }, 600000); // 10 minutes

    return () => {
      isMounted = false;
      clearInterval(refreshInterval);
    };
  }, [lectureId, isOpen, inline]);

  if (!isOpen && !inline) return null;

  const handleContextMenu = (e: React.MouseEvent) => {
    e.preventDefault();
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if ((e.ctrlKey && (e.key === "s" || e.key === "S" || e.key === "u" || e.key === "U")) || e.key === "F12") {
      e.preventDefault();
      e.stopPropagation();
    }
  };

  const content = (
    <div 
      className={`flex flex-col bg-cw-surface border border-cw-border rounded-2xl overflow-hidden shadow-2xl select-none relative ${inline ? "w-full h-[700px]" : "w-full max-w-5xl h-[88vh]"}`}
      onContextMenu={handleContextMenu}
      onKeyDown={handleKeyDown}
      tabIndex={0}
    >
      {/* Header */}
      <div className="flex items-center justify-between px-6 py-3.5 border-b border-cw-border bg-cw-elevated/60">
        <div className="flex items-center gap-3 min-w-0">
          <div className="w-8 h-8 rounded-lg bg-emerald-500/10 border border-emerald-500/20 flex items-center justify-center text-emerald-400 flex-shrink-0">
            <FileText className="w-4 h-4" />
          </div>
          <div className="min-w-0">
            <h3 className="font-bold text-sm sm:text-base text-white truncate">
              {title}
            </h3>
            <div className="flex items-center gap-2 text-xs text-cw-muted">
              {pageCount > 0 && <span>{pageCount} Pages &bull; </span>}
              <span className="flex items-center gap-1 text-emerald-400 font-semibold">
                <ShieldCheck className="w-3.5 h-3.5" />
                Protected Course Wallah Notes
              </span>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-2 sm:gap-3 flex-shrink-0">
          {downloadUrl && (
            <a
              href={downloadUrl}
              target="_blank"
              rel="noreferrer"
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-emerald-600/20 hover:bg-emerald-600/30 border border-emerald-500/30 text-emerald-400 text-xs font-semibold transition-all"
              title="Open or Download PDF"
            >
              <span>Download PDF</span>
            </a>
          )}
          <span className="hidden sm:inline-flex text-[11px] text-cw-muted font-mono px-2 py-0.5 rounded bg-cw-surface border border-cw-border">
            Session Active
          </span>
          {onClose && (
            <button
              onClick={onClose}
              className="p-1.5 rounded-lg text-cw-muted hover:text-white hover:bg-cw-elevated transition-colors"
              aria-label="Close Notes Reader"
            >
              <X className="w-5 h-5" />
            </button>
          )}
        </div>
      </div>

      {/* Reader Body */}
      <div className="flex-1 bg-cw-bg relative overflow-hidden">
        {loading && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 z-10 bg-cw-surface">
            <Loader2 className="w-8 h-8 text-cw-primary animate-spin" />
            <p className="text-xs text-cw-muted font-medium">Authorizing Secure Document Access...</p>
          </div>
        )}

        {error && (
          <div className="absolute inset-0 flex flex-col items-center justify-center p-6 text-center space-y-3 z-10 bg-cw-surface">
            <AlertCircle className="w-12 h-12 text-rose-400" />
            <h4 className="text-base font-bold text-white">Document Access Unavailable</h4>
            <p className="text-xs text-cw-muted max-w-sm">{error}</p>
            <div className="flex items-center gap-2 pt-2">
              <button
                onClick={() => loadPdfAccess(true)}
                className="px-4 py-2 bg-cw-primary text-white text-xs font-bold rounded-xl flex items-center gap-1.5"
              >
                <RefreshCw className="w-3.5 h-3.5" />
                <span>Retry</span>
              </button>
              {onClose && (
                <button
                  onClick={onClose}
                  className="px-4 py-2 bg-cw-surface hover:bg-cw-elevated border border-cw-border text-white text-xs rounded-xl"
                >
                  Close Reader
                </button>
              )}
            </div>
          </div>
        )}

        {/* Ambient Subtle Course Wallah Brand Watermark */}
        <div className="absolute bottom-4 right-6 pointer-events-none z-10 text-[10px] font-mono font-semibold text-white/20 tracking-wider">
          COURSE WALLAH &bull; VERIFIED STUDENT STUDY RESOURCE
        </div>

        {downloadUrl && !loading && (
          <iframe
            src={`${downloadUrl}#toolbar=0&navpanes=0&scrollbar=1`}
            title={title}
            className="w-full h-full border-0"
          />
        )}
      </div>
    </div>
  );

  if (inline) {
    return content;
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 sm:p-6 bg-black/80 backdrop-blur-sm animate-fade-in">
      {content}
    </div>
  );
}
