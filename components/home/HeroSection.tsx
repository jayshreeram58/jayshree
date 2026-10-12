"use client";

import { motion } from "framer-motion";
import { ArrowUpRight, BookOpen, CheckCircle2, Command, GraduationCap, Search, ShieldCheck, Sparkles, Zap } from "lucide-react";

interface HeroSectionProps {
  searchQuery: string;
  setSearchQuery: (query: string) => void;
  onSearchSubmit: (e: React.FormEvent) => void;
  appCount: number;
  batchCount: number;
}

export function HeroSection({ searchQuery, setSearchQuery, onSearchSubmit, appCount, batchCount }: HeroSectionProps) {
  return (
    <section className="relative isolate overflow-hidden rounded-[2rem] border border-white/10 bg-[#0c1224] shadow-[0_30px_100px_-40px_rgba(37,99,235,.55)]">
      <div aria-hidden="true" className="absolute inset-0 -z-10 overflow-hidden">
        <div className="absolute -right-24 -top-36 h-[28rem] w-[28rem] rounded-full bg-blue-600/25 blur-[100px]" />
        <div className="absolute -bottom-48 left-1/4 h-[25rem] w-[25rem] rounded-full bg-violet-600/20 blur-[100px]" />
        <div className="absolute inset-0 opacity-[.14]" style={{ backgroundImage: "linear-gradient(rgba(148,163,184,.22) 1px, transparent 1px), linear-gradient(90deg, rgba(148,163,184,.22) 1px, transparent 1px)", backgroundSize: "36px 36px", maskImage: "linear-gradient(to bottom, black, transparent 90%)" }} />
      </div>

      <div className="grid gap-10 px-6 py-10 sm:px-10 sm:py-14 lg:grid-cols-[1.15fr_.85fr] lg:items-center lg:px-14 lg:py-16">
        <motion.div initial={{ opacity: 0, y: 18 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: .55 }} className="relative z-10">
          <div className="mb-7 inline-flex items-center gap-2 rounded-full border border-cyan-300/20 bg-cyan-300/[.07] px-3.5 py-2 text-[11px] font-bold uppercase tracking-[.16em] text-cyan-200">
            <span className="relative flex h-2 w-2"><span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-70" /><span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-400" /></span>
            Your learning space, upgraded
          </div>

          <h1 className="max-w-3xl text-4xl font-black leading-[1.08] tracking-[-.045em] text-white sm:text-5xl lg:text-[4.1rem]">
            Learn with focus.
            <span className="mt-1 block bg-gradient-to-r from-cyan-300 via-blue-400 to-violet-400 bg-clip-text text-transparent">Grow without limits.</span>
          </h1>
          <p className="mt-6 max-w-xl text-sm leading-7 text-slate-300 sm:text-base">
            Courses, structured batches, lecture streams and study notes — all in one calm, distraction-free workspace built for your next big goal.
          </p>

          <form onSubmit={onSearchSubmit} className="mt-8 flex max-w-2xl items-center gap-2 rounded-2xl border border-white/10 bg-black/25 p-2 shadow-2xl shadow-black/20 backdrop-blur-xl focus-within:border-cyan-300/40">
            <Search className="ml-3 h-5 w-5 shrink-0 text-slate-400" />
            <input
              type="search"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Find a course, batch or subject…"
              aria-label="Search courses, batches or subjects"
              className="min-w-0 flex-1 bg-transparent px-1 py-3 text-sm text-white outline-none placeholder:text-slate-500"
            />
            <button type="submit" className="inline-flex shrink-0 items-center gap-2 rounded-xl bg-white px-4 py-3 text-xs font-extrabold text-slate-950 transition hover:bg-cyan-100 sm:px-5 sm:text-sm">
              Explore <ArrowUpRight className="h-4 w-4" />
            </button>
          </form>

          <div className="mt-7 flex flex-wrap items-center gap-x-5 gap-y-3 text-xs font-medium text-slate-400">
            <span className="inline-flex items-center gap-2"><ShieldCheck className="h-4 w-4 text-emerald-300" /> Focus-first learning</span>
            <span className="inline-flex items-center gap-2"><Zap className="h-4 w-4 text-amber-300" /> Quick access</span>
            <span className="inline-flex items-center gap-2"><BookOpen className="h-4 w-4 text-cyan-300" /> Notes with lectures</span>
          </div>
        </motion.div>

        <motion.div initial={{ opacity: 0, y: 22, scale: .98 }} animate={{ opacity: 1, y: 0, scale: 1 }} transition={{ duration: .65, delay: .12 }} className="relative mx-auto w-full max-w-md lg:ml-auto">
          <div className="absolute -inset-4 rounded-[2rem] bg-gradient-to-br from-blue-500/20 via-transparent to-violet-500/20 blur-2xl" />
          <div className="relative rounded-[1.7rem] border border-white/10 bg-slate-950/75 p-5 shadow-2xl backdrop-blur-2xl sm:p-6">
            <div className="flex items-center justify-between border-b border-white/10 pb-5">
              <div className="flex items-center gap-3">
                <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-gradient-to-br from-cyan-300 to-blue-600 text-slate-950 shadow-lg shadow-blue-500/20"><GraduationCap className="h-6 w-6" /></div>
                <div><p className="text-sm font-bold text-white">Your learning hub</p><p className="mt-0.5 text-[11px] text-slate-500">One step at a time</p></div>
              </div>
              <span className="rounded-full border border-emerald-400/20 bg-emerald-400/10 px-2.5 py-1 text-[10px] font-bold text-emerald-300">READY TO LEARN</span>
            </div>

            <div className="mt-5 grid grid-cols-2 gap-3">
              <div className="rounded-2xl border border-white/[.08] bg-white/[.035] p-4">
                <div className="mb-4 flex h-9 w-9 items-center justify-center rounded-xl bg-blue-400/10 text-blue-300"><Command className="h-4 w-4" /></div>
                <p className="text-2xl font-black tracking-tight text-white">{appCount}</p><p className="mt-1 text-xs text-slate-400">Learning tracks</p>
              </div>
              <div className="rounded-2xl border border-white/[.08] bg-white/[.035] p-4">
                <div className="mb-4 flex h-9 w-9 items-center justify-center rounded-xl bg-violet-400/10 text-violet-300"><BookOpen className="h-4 w-4" /></div>
                <p className="text-2xl font-black tracking-tight text-white">{batchCount}</p><p className="mt-1 text-xs text-slate-400">Available batches</p>
              </div>
            </div>

            <div className="mt-4 rounded-2xl border border-blue-300/15 bg-gradient-to-r from-blue-500/10 to-violet-500/10 p-4">
              <div className="flex items-start gap-3">
                <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-cyan-300/10 text-cyan-200"><Sparkles className="h-4 w-4" /></div>
                <div><p className="text-sm font-bold text-white">Made for your momentum</p><p className="mt-1 text-xs leading-5 text-slate-400">Pick a track, open a batch, and keep your learning organized from one place.</p></div>
                <CheckCircle2 className="mt-1 h-4 w-4 shrink-0 text-emerald-300" />
              </div>
            </div>
            <div className="mt-5 flex items-center justify-between text-[10px] font-semibold uppercase tracking-[.15em] text-slate-500"><span>Course Wallah</span><span>Study • Practice • Progress</span></div>
          </div>
          <div className="absolute -bottom-4 -left-3 hidden items-center gap-2 rounded-2xl border border-white/10 bg-slate-900/90 px-4 py-3 shadow-xl backdrop-blur-xl sm:flex"><span className="h-2 w-2 rounded-full bg-emerald-400" /><span className="text-xs font-semibold text-slate-200">Your next chapter starts here</span></div>
        </motion.div>
      </div>
    </section>
  );
}
