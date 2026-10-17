import time
import logging
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, Tuple, List
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from db.connection import get_db_session
from db.repository import ContentRepository, slugify
from db.models import App, Batch
from parsers.indexer import TxtIndexer, NormalizedBatchTree, NormalizedLecture
from bot.progress_ui import TelegramProgressUI

logger = logging.getLogger(__name__)

@dataclass
class BatchWizardState:
    bot_id: str
    user_id: int
    step: str = "IDLE"
    history: List[str] = field(default_factory=list)
    
    # TXT data
    filename: str = ""
    file_content: str = ""
    tree: Optional[NormalizedBatchTree] = None
    analysis_stats: Dict[str, Any] = field(default_factory=dict)
    
    # App State
    app_id: Optional[str] = None
    app_name: str = "Course Wallah"
    app_description: Optional[str] = None
    app_icon_url: Optional[str] = None
    
    # Batch State
    batch_id: Optional[str] = None
    batch_name: str = "Academic Batch"
    category: str = "B.Tech"
    branch: str = "CSE"
    semester: str = "3rd Semester"
    academic_year: str = "2026"
    thumbnail_url: str = ""
    quality_pref: str = "AUTO / BEST"
    watermark_enabled: bool = True
    execution_mode: str = "APPEND NEW CONTENT"
    start_index: int = 1
    retry_failed_only: bool = False
    
    # Editing / Mapping buffer
    mapping_overrides: Dict[str, Any] = field(default_factory=dict)
    text_input_prompt: Optional[str] = None
    created_at: float = field(default_factory=time.time)

    def push_step(self, next_step: str):
        if self.step and self.step != next_step:
            self.history.append(self.step)
        self.step = next_step

    def pop_step(self) -> str:
        if self.history:
            self.step = self.history.pop()
        else:
            self.step = "ANALYSIS_PREVIEW" if self.tree else "IDLE"
        return self.step


class BatchWizardManager:
    """
    Manages interactive multi-step wizard state machine, TXT analysis, App/Batch selection,
    mapping previews, and keyboard markups for Course Wallah Platform.
    """

    _wizard_sessions: Dict[Tuple[str, int], BatchWizardState] = {}

    @classmethod
    def get_session_key(cls, bot_id: str, user_id: int) -> Tuple[str, int]:
        return (str(bot_id), int(user_id))

    @classmethod
    def get_session(cls, bot_id: str, user_id: int) -> Optional[BatchWizardState]:
        key = cls.get_session_key(bot_id, user_id)
        return cls._wizard_sessions.get(key)

    @classmethod
    def get_or_create_session(cls, bot_id: str, user_id: int) -> BatchWizardState:
        key = cls.get_session_key(bot_id, user_id)
        if key not in cls._wizard_sessions:
            cls._wizard_sessions[key] = BatchWizardState(bot_id=str(bot_id), user_id=int(user_id))
        return cls._wizard_sessions[key]

    @classmethod
    def clear_session(cls, bot_id: str, user_id: int):
        key = cls.get_session_key(bot_id, user_id)
        if key in cls._wizard_sessions:
            del cls._wizard_sessions[key]

    @classmethod
    async def analyze_and_start_session(
        cls,
        bot_id: str,
        user_id: int,
        file_content: str,
        filename: str
    ) -> BatchWizardState:
        """
        Parses TXT, creates or updates BatchWizardState, and performs DB analysis.
        """
        tree: NormalizedBatchTree = TxtIndexer.index_txt(file_content, filename=filename)
        state = cls.get_or_create_session(bot_id, user_id)
        
        state.filename = filename
        state.file_content = file_content
        state.tree = tree
        state.batch_name = tree.batch_name or "Academic Batch"
        state.app_name = tree.app_name or "Course Wallah"
        state.category = tree.category or "B.Tech"
        state.branch = tree.branch or "CSE"
        state.semester = tree.semester or "3rd Semester"
        state.academic_year = tree.academic_year or "2026"
        state.thumbnail_url = tree.thumbnail_url or ""
        state.step = "ANALYSIS_PREVIEW"
        state.history.clear()

        total_lectures = tree.total_lectures
        video_count = 0
        pdf_count = 0
        combo_count = 0
        providers: Dict[str, int] = {}

        for s in tree.subjects:
            for f in s.folders:
                for lec in f.lectures:
                    has_v = bool(lec.video_url)
                    has_p = bool(lec.pdf_url)
                    if has_v: video_count += 1
                    if has_p: pdf_count += 1
                    if has_v and has_p: combo_count += 1

                    p_name = (lec.provider or "direct").lower()
                    if "appx" in p_name: p_name = "APPX"
                    elif "youtube" in p_name: p_name = "YouTube"
                    elif "spayee" in p_name: p_name = "Spayee"
                    else: p_name = p_name.upper()

                    providers[p_name] = providers.get(p_name, 0) + 1

        unit_count = sum(len(s.folders) for s in tree.subjects)
        subject_count = len(tree.subjects)

        # Check DB for existing default app & batch
        already_processed_count = 0
        async with get_db_session() as session:
            repo = ContentRepository(session)
            apps = await repo.get_all_apps()
            if apps:
                default_app = apps[0]
                state.app_id = default_app.id
                state.app_name = default_app.name

                existing_batch = await repo.find_existing_batch(default_app.id, state.batch_name)
                if existing_batch:
                    state.batch_id = existing_batch.id
                    summary = await repo.get_batch_lecture_summary(existing_batch.id)
                    already_processed_count = summary.get("published_lectures", 0)

        new_count = max(0, total_lectures - already_processed_count)

        state.analysis_stats = {
            "filename": filename,
            "batch_name": state.batch_name,
            "total_lectures": total_lectures,
            "video_count": video_count,
            "pdf_count": pdf_count,
            "combo_count": combo_count,
            "unit_count": unit_count,
            "subject_count": subject_count,
            "new_count": new_count,
            "already_processed_count": already_processed_count,
            "invalid_count": 0,
            "providers": providers
        }

        return state

    # ==========================================
    # MARKUP GENERATORS (NO INVALID URLS)
    # ==========================================

    @classmethod
    def build_welcome_markup(cls, is_admin: bool) -> InlineKeyboardMarkup:
        if is_admin:
            return InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📦 Upload Batch", callback_data="wizard:start_upload"),
                    InlineKeyboardButton("📊 Batch Status", callback_data="admin:status")
                ],
                [
                    InlineKeyboardButton("🎬 YouTube Accounts", callback_data="admin:youtube_accounts"),
                    InlineKeyboardButton("🛠 Admin Panel", callback_data="admin:dashboard")
                ],
                [
                    InlineKeyboardButton("ℹ️ Help & Guide", callback_data="admin:help")
                ]
            ])
        else:
            return InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("ℹ️ Help", callback_data="user:help"),
                    InlineKeyboardButton("🆔 My ID", callback_data="user:id")
                ]
            ])

    @classmethod
    def build_admin_dashboard_markup(cls) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📦 Batch Upload", callback_data="wizard:start_upload"),
                InlineKeyboardButton("📊 Batch Status", callback_data="admin:status"),
                InlineKeyboardButton("📋 Batch Jobs", callback_data="admin:jobs")
            ],
            [
                InlineKeyboardButton("📱 Manage Apps & Batches", callback_data="manage:apps_list"),
                InlineKeyboardButton("🎬 YouTube Accounts", callback_data="admin:youtube_accounts")
            ],
            [
                InlineKeyboardButton("📜 Logs", callback_data="admin:logs")
            ],
            [
                InlineKeyboardButton("🔄 Resume", callback_data="job:resume_active"),
                InlineKeyboardButton("⏸ Pause", callback_data="job:pause_active"),
                InlineKeyboardButton("❌ Cancel", callback_data="job:cancel_active")
            ],
            [
                InlineKeyboardButton("🔁 Retry Failed", callback_data="job:retry_active"),
                InlineKeyboardButton("⬅️ Close", callback_data="admin:close")
            ]
        ])


    @classmethod
    def build_analysis_markup(cls) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📱 SELECT EXISTING APP", callback_data="wizard:step_select_app"),
                InlineKeyboardButton("➕ CREATE NEW APP", callback_data="wizard:step_create_app")
            ],
            [
                InlineKeyboardButton("✏️ EDIT / MAP CONTENT", callback_data="wizard:step_map_menu"),
                InlineKeyboardButton("❌ CANCEL", callback_data="wizard:cancel")
            ]
        ])

    @classmethod
    def build_app_selection_markup(cls, apps: List[App]) -> InlineKeyboardMarkup:
        buttons = []
        for app in apps:
            buttons.append([InlineKeyboardButton(f"🎓 {app.name}", callback_data=f"wizard:app_select:{app.id}")])

        buttons.append([
            InlineKeyboardButton("➕ CREATE NEW APP", callback_data="wizard:step_create_app"),
            InlineKeyboardButton("✏️ EDIT APP", callback_data="wizard:step_edit_app")
        ])
        buttons.append([
            InlineKeyboardButton("⬅️ BACK", callback_data="wizard:back"),
            InlineKeyboardButton("❌ CANCEL", callback_data="wizard:cancel")
        ])
        return InlineKeyboardMarkup(buttons)

    @classmethod
    def build_app_create_markup(cls, step: str) -> InlineKeyboardMarkup:
        if step == "NAME":
            return InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("⬅️ BACK", callback_data="wizard:back"),
                    InlineKeyboardButton("❌ CANCEL", callback_data="wizard:cancel")
                ]
            ])
        elif step in ("DESC", "ICON"):
            return InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("⏭ SKIP", callback_data=f"wizard:app_skip_{step.lower()}"),
                    InlineKeyboardButton("⬅️ BACK", callback_data="wizard:back"),
                    InlineKeyboardButton("❌ CANCEL", callback_data="wizard:cancel")
                ]
            ])
        else: # Summary
            return InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ CREATE APP", callback_data="wizard:app_create_confirm"),
                    InlineKeyboardButton("✏️ EDIT", callback_data="wizard:app_edit_fields")
                ],
                [
                    InlineKeyboardButton("⬅️ BACK", callback_data="wizard:back"),
                    InlineKeyboardButton("❌ CANCEL", callback_data="wizard:cancel")
                ]
            ])

    @classmethod
    def build_app_edit_markup(cls) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✏️ Change Name", callback_data="wizard:app_edit_name"),
                InlineKeyboardButton("📝 Change Description", callback_data="wizard:app_edit_desc")
            ],
            [
                InlineKeyboardButton("🖼 Change Thumbnail", callback_data="wizard:app_edit_icon"),
                InlineKeyboardButton("✅ SAVE", callback_data="wizard:app_save_edit")
            ],
            [
                InlineKeyboardButton("⬅️ BACK", callback_data="wizard:back"),
                InlineKeyboardButton("❌ CANCEL", callback_data="wizard:cancel")
            ]
        ])

    @classmethod
    def build_batch_selection_markup(cls, batches: List[Batch]) -> InlineKeyboardMarkup:
        buttons = []
        for b in batches:
            buttons.append([InlineKeyboardButton(f"📦 {b.name}", callback_data=f"wizard:batch_select:{b.id}")])

        buttons.append([
            InlineKeyboardButton("➕ CREATE NEW BATCH", callback_data="wizard:step_create_batch"),
            InlineKeyboardButton("✏️ EDIT / MAP TXT", callback_data="wizard:step_map_menu")
        ])
        buttons.append([
            InlineKeyboardButton("⬅️ BACK", callback_data="wizard:back"),
            InlineKeyboardButton("❌ CANCEL", callback_data="wizard:cancel")
        ])
        return InlineKeyboardMarkup(buttons)

    @classmethod
    def build_existing_batch_markup(cls, has_failed: bool = False) -> InlineKeyboardMarkup:
        buttons = [
            [
                InlineKeyboardButton("➕ APPEND NEW CONTENT", callback_data="wizard:mode_append"),
                InlineKeyboardButton("▶️ START PROCESSING", callback_data="wizard:step_confirm")
            ]
        ]
        if has_failed:
            buttons.append([InlineKeyboardButton("🔄 REPROCESS FAILED", callback_data="wizard:mode_retry_failed")])

        buttons.append([
            InlineKeyboardButton("✏️ EDIT / MAP", callback_data="wizard:step_map_menu"),
            InlineKeyboardButton("⬅️ BACK", callback_data="wizard:back"),
            InlineKeyboardButton("❌ CANCEL", callback_data="wizard:cancel")
        ])
        return InlineKeyboardMarkup(buttons)

    @classmethod
    def build_batch_create_wizard_markup(cls, step: str) -> InlineKeyboardMarkup:
        if step == "CATEGORY":
            return InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🎓 B.Tech", callback_data="wizard:set_cat_B.Tech"),
                    InlineKeyboardButton("📚 Class 12", callback_data="wizard:set_cat_Class 12")
                ],
                [
                    InlineKeyboardButton("📖 Class 11", callback_data="wizard:set_cat_Class 11"),
                    InlineKeyboardButton("📝 Other", callback_data="wizard:set_cat_Other")
                ],
                [
                    InlineKeyboardButton("⬅️ BACK", callback_data="wizard:back"),
                    InlineKeyboardButton("❌ CANCEL", callback_data="wizard:cancel")
                ]
            ])
        elif step == "BRANCH":
            return InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("CSE", callback_data="wizard:set_branch_CSE"),
                    InlineKeyboardButton("IT", callback_data="wizard:set_branch_IT"),
                    InlineKeyboardButton("ECE", callback_data="wizard:set_branch_ECE")
                ],
                [
                    InlineKeyboardButton("EEE", callback_data="wizard:set_branch_EEE"),
                    InlineKeyboardButton("ME", callback_data="wizard:set_branch_ME"),
                    InlineKeyboardButton("OTHER", callback_data="wizard:set_branch_OTHER")
                ],
                [
                    InlineKeyboardButton("⬅️ BACK", callback_data="wizard:back"),
                    InlineKeyboardButton("❌ CANCEL", callback_data="wizard:cancel")
                ]
            ])
        elif step == "SEMESTER":
            return InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("1st", callback_data="wizard:set_sem_1st"),
                    InlineKeyboardButton("2nd", callback_data="wizard:set_sem_2nd"),
                    InlineKeyboardButton("3rd", callback_data="wizard:set_sem_3rd"),
                    InlineKeyboardButton("4th", callback_data="wizard:set_sem_4th")
                ],
                [
                    InlineKeyboardButton("5th", callback_data="wizard:set_sem_5th"),
                    InlineKeyboardButton("6th", callback_data="wizard:set_sem_6th"),
                    InlineKeyboardButton("7th", callback_data="wizard:set_sem_7th"),
                    InlineKeyboardButton("8th", callback_data="wizard:set_sem_8th")
                ],
                [
                    InlineKeyboardButton("⬅️ BACK", callback_data="wizard:back"),
                    InlineKeyboardButton("❌ CANCEL", callback_data="wizard:cancel")
                ]
            ])
        elif step == "SETTINGS":
            return InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("⚡ Quality: BEST", callback_data="wizard:set_qual_AUTO"),
                    InlineKeyboardButton("1080p", callback_data="wizard:set_qual_1080p"),
                    InlineKeyboardButton("720p", callback_data="wizard:set_qual_720p")
                ],
                [
                    InlineKeyboardButton("💧 Watermark: ON", callback_data="wizard:set_wm_on"),
                    InlineKeyboardButton("❌ OFF", callback_data="wizard:set_wm_off")
                ],
                [
                    InlineKeyboardButton("✅ CONFIRM SETTINGS", callback_data="wizard:step_confirm"),
                    InlineKeyboardButton("⬅️ BACK", callback_data="wizard:back")
                ]
            ])
        else: # Confirmation
            return InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ CONFIRM BATCH", callback_data="wizard:batch_create_confirm"),
                    InlineKeyboardButton("✏️ EDIT", callback_data="wizard:step_create_batch")
                ],
                [
                    InlineKeyboardButton("⬅️ BACK", callback_data="wizard:back"),
                    InlineKeyboardButton("❌ CANCEL", callback_data="wizard:cancel")
                ]
            ])

    @classmethod
    def build_mapping_menu_markup(cls) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📱 App Mapping", callback_data="wizard:map_app"),
                InlineKeyboardButton("📦 Batch Mapping", callback_data="wizard:map_batch")
            ],
            [
                InlineKeyboardButton("📚 Subject Mapping", callback_data="wizard:map_subject"),
                InlineKeyboardButton("📁 Folder Mapping", callback_data="wizard:map_folder")
            ],
            [
                InlineKeyboardButton("🎬 Lecture Mapping", callback_data="wizard:map_lecture"),
                InlineKeyboardButton("🔗 Video URL", callback_data="wizard:map_video")
            ],
            [
                InlineKeyboardButton("👀 Preview Parsed Data", callback_data="wizard:map_preview"),
                InlineKeyboardButton("🔄 Re-Analyze TXT", callback_data="wizard:map_reanalyze")
            ],
            [
                InlineKeyboardButton("✅ Confirm Mapping", callback_data="wizard:step_confirm"),
                InlineKeyboardButton("⬅️ BACK", callback_data="wizard:back"),
                InlineKeyboardButton("❌ CANCEL", callback_data="wizard:cancel")
            ]
        ])

    @classmethod
    def build_parsed_preview_markup(cls) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirm Mapping", callback_data="wizard:step_confirm"),
                InlineKeyboardButton("⬅️ BACK", callback_data="wizard:back"),
                InlineKeyboardButton("❌ CANCEL", callback_data="wizard:cancel")
            ]
        ])

    @classmethod
    def build_final_confirmation_markup(cls) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🚀 START PROCESSING", callback_data="wizard:start_processing")
            ],
            [
                InlineKeyboardButton("✏️ EDIT / MAP", callback_data="wizard:step_map_menu"),
                InlineKeyboardButton("⬅️ BACK", callback_data="wizard:back"),
                InlineKeyboardButton("❌ CANCEL", callback_data="wizard:cancel")
            ]
        ])

    @classmethod
    def build_batch_status_markup(cls) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔄 REFRESH", callback_data="admin:status"),
                InlineKeyboardButton("⏸ PAUSE", callback_data="job:pause_active"),
                InlineKeyboardButton("❌ CANCEL", callback_data="job:cancel_active")
            ],
            [
                InlineKeyboardButton("⬅️ BACK", callback_data="admin:dashboard")
            ]
        ])

    @classmethod
    def build_batch_jobs_markup(cls) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔄 REFRESH", callback_data="admin:jobs"),
                InlineKeyboardButton("⬅️ BACK", callback_data="admin:dashboard")
            ]
        ])

    @classmethod
    def build_paused_batch_markup(cls, job_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("▶️ Resume Ingestion", callback_data=f"job:resume:{job_id}"),
                InlineKeyboardButton("🛑 Cancel Job", callback_data=f"job:prompt_cancel:{job_id}")
            ]
        ])

    @classmethod
    def build_cancel_confirmation_markup(cls, job_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ YES, CANCEL", callback_data=f"job:confirm_cancel:{job_id}"),
                InlineKeyboardButton("↩️ KEEP RUNNING", callback_data=f"job:keep_running:{job_id}")
            ]
        ])

    @classmethod
    def build_running_batch_markup(cls, job_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⏸ Pause", callback_data="job:pause_active"),
                InlineKeyboardButton("🛑 Cancel", callback_data=f"job:prompt_cancel:{job_id}")
            ]
        ])

    @classmethod
    def build_existing_app_found_markup(cls, app_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ USE THIS APP", callback_data=f"wizard:app_select:{app_id}"),
                InlineKeyboardButton("✏️ CHOOSE DIFFERENT APP", callback_data="wizard:step_select_app")
            ],
            [
                InlineKeyboardButton("➕ CREATE NEW APP", callback_data="wizard:step_create_app"),
                InlineKeyboardButton("❌ CANCEL", callback_data="wizard:cancel")
            ]
        ])

    @classmethod
    def build_app_not_found_markup(cls) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("➕ CREATE NEW APP", callback_data="wizard:step_create_app"),
                InlineKeyboardButton("📱 SELECT EXISTING APP", callback_data="wizard:step_select_app")
            ],
            [
                InlineKeyboardButton("✏️ EDIT / MAP", callback_data="wizard:step_map_menu"),
                InlineKeyboardButton("❌ CANCEL", callback_data="wizard:cancel")
            ]
        ])

    @classmethod
    def build_existing_batch_found_markup(cls, batch_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ USE THIS BATCH", callback_data=f"wizard:batch_select:{batch_id}"),
                InlineKeyboardButton("✏️ EDIT / MAP", callback_data="wizard:step_map_menu")
            ],
            [
                InlineKeyboardButton("➕ CREATE NEW BATCH", callback_data="wizard:step_create_batch"),
                InlineKeyboardButton("⬅️ BACK", callback_data="wizard:back")
            ],
            [
                InlineKeyboardButton("❌ CANCEL", callback_data="wizard:cancel")
            ]
        ])

    @classmethod
    def build_new_batch_prompt_markup(cls) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("➕ CREATE BATCH", callback_data="wizard:step_create_batch"),
                InlineKeyboardButton("📦 SELECT EXISTING BATCH", callback_data="wizard:step_select_batch")
            ],
            [
                InlineKeyboardButton("✏️ EDIT / MAP", callback_data="wizard:step_map_menu"),
                InlineKeyboardButton("⬅️ BACK", callback_data="wizard:back")
            ],
            [
                InlineKeyboardButton("❌ CANCEL", callback_data="wizard:cancel")
            ]
        ])

    @classmethod
    def build_completed_batch_markup(cls, batch_id: str, has_failed: bool = False) -> InlineKeyboardMarkup:
        buttons = []
        if has_failed:
            buttons.append([
                InlineKeyboardButton("🔁 RETRY FAILED", callback_data=f"job:retry:{batch_id}"),
                InlineKeyboardButton("📋 VIEW FAILED", callback_data=f"job:view_failed:{batch_id}")
            ])
        buttons.append([
            InlineKeyboardButton("📊 BATCH DETAILS", callback_data=f"admin:batch_details:{batch_id}"),
            InlineKeyboardButton("👑 ADMIN PANEL", callback_data="admin:dashboard")
        ])
        return InlineKeyboardMarkup(buttons)
