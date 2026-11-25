// The app's names for operator_api's schemas. Every shape comes from the
// generated src/lib/openapi.d.ts (npm run gen:api), so a contract change
// shows up as a type error here, not as a blank screen.

import type { components } from "./openapi";

type S = components["schemas"];

export type Source = S["Source"];
export type SourceSystem = Source["system"];
export type SourceView = S["SourceView"];
export type SourceRecords = S["SourceRecords"];
export type Fact = S["Fact"];
export type NotFoundItem = S["NotFound"];
export type Hidden = S["Hidden"];
export type HiddenByOwner = S["HiddenOwner"];
export type Conflict = S["Conflict"];
export type ConflictValue = S["ConflictValue"];
export type Freshness = S["Freshness"];
export type ObjectRef = S["ObjectRef"];
export type LinkGroup = S["LinkGroup"];
export type LinkRef = S["LinkRef"];
export type Candidate = S["Candidate"];
export type UnderstoodEntity = S["UnderstoodEntity"];
export type Understanding = S["UnderstandingEvent"];
export type Intent = Understanding["intent"];
export type StatusEvent = S["StatusEvent"];
export type SourcesEvent = S["SourcesEvent"];
export type TokenEvent = S["TokenEvent"];
export type VerdictEvent = S["VerdictEvent"];
export type ChartsEvent = S["ChartsEvent"];
export type Answer = S["AnswerPayload"];
export type MeasureDefinition = S["MeasureCited"];
export type QueryWork = S["QueryWork"];
export type FilterEdit = S["FilterEdit"];
export type StatementRead = S["StatementRead"];
export type LabourPage = S["LabourPage"];
export type LabourWeek = S["LabourWeek"];
export type Profitability = S["Profitability"];
export type PersonKnows = S["PersonKnows"];
export type KnownRecord = S["KnownRecord"];
export type RecordDocuments = S["RecordDocuments"];
export type DocList = S["DocList"];
export type DocRow = S["DocRow"];
export type ProductFormula = S["ProductFormula"];
export type LearnedTemplates = S["LearnedTemplates"];
export type LearnedTemplate = S["LearnedTemplate"];
export type FormulaSheet = S["FormulaSheet"];
export type FormulaComponent = S["FormulaComponent"];
export type MaterialCost = S["MaterialCost"];
export type CostBreakdownView = S["CostBreakdown"];
export type CostLine = S["CostLine"];
export type CostSource = S["CostSource"];
export type OrderMargin = S["OrderMargin"];
export type ProductMargin = S["ProductMargin"];
export type ProfitDriver = S["ProfitDriver"];
export type ApiErrorBody = S["ErrorBody"];
export type AskRequest = S["AskRequest"];
export type HistoryTurn = S["HistoryTurn"];
export type Me = S["Me"];
export type AuthConfig = S["AuthConfig"];
export type EntraConfig = S["EntraConfig"];
export type DevLoginRequest = S["DevLoginRequest"];
export type DevLoginResponse = S["DevLoginResponse"];
export type Suggestion = S["Suggestion"];
export type SuggestResponse = S["SuggestResponse"];
export type BizPage = S["BizPage"];
export type BizTable = S["BizTable"];
export type BizCell = S["BizCell"];
export type BizKind = BizPage["kind"];
export type RecordList = S["RecordList"];
export type RecordViews = S["RecordViews"];
export type HomeResponse = S["Home"];
export type HomeNeeds = S["HomeNeeds"];
export type HomeNeedItem = S["HomeNeedItem"];
export type HomeNeedFacet = S["HomeNeedFacet"];
export type HomeNeedPart = S["HomeNeedPart"];
export type SidebarResponse = S["Sidebar"];
export type NeedsAttention = S["NeedsAttention"];
export type LatePo = S["LatePo"];
export type LateList = S["LateList"];
export type PoClosure = S["PoClosure"];
export type DataHealth = S["DataHealth"];
export type DataHealthCheck = S["DataHealthCheck"];
export type CustomerSummary = S["CustomerSummary"];
export type CustomerAmount = S["CustomerAmount"];
export type OrderRow = S["OrderRow"];
export type AtRiskItem = S["AtRisk"];
export type RecentQuestion = S["RecentQuestion"];
export type ObjectPage = S["ObjectPage"];
export type Explore = S["Explore"];
export type ExploreGroup = S["ExploreGroup"];
export type ExploreNode = S["ExploreNode"];
export type RecordHistory = S["RecordHistory"];
export type HistoryEntry = S["HistoryEntry"];
export type HistoryChange = S["HistoryChange"];
export type RecordAsOf = S["RecordAsOf"];
export type ObjectSection = S["ObjectSection"];
export type ImpactPanel = S["ImpactPanel"];
export type SectionName = ObjectSection["section"];
export type ObjectStatus = S["ObjectStatus"];
export type TimelineEvent = S["TimelineEvent"];
export type LinkedItem = S["LinkedItem"];
export type Person = S["Person"];
export type Money = S["Money"];
export type LandedCost = S["LandedCost"];
export type LateLine = S["LateLine"];
export type EvidenceTrail = S["EvidenceTrail"];
export type PoTrail = S["PoTrail"];
export type TrailStep = S["TrailStep"];
export type EtaCheck = S["EtaCheck"];
export type VendorPromise = S["VendorPromise"];
export type PromiseLine = S["PromiseLine"];
export type NextAction = S["NextAction"];
export type MaterialRow = S["MaterialRow"];
export type TrailGap = S["TrailGap"];
export type Briefing = S["Briefing"];
export type BriefingItem = S["BriefingItem"];
export type BriefingSeen = S["BriefingSeen"];
export type WatcherItemState = S["WatcherItemState"];
export type WatcherItemStateRequest = S["WatcherItemStateRequest"];
export type BriefingItems = S["BriefingItems"];
export type BriefingPages = S["BriefingPages"];
export type BriefingDataNote = S["BriefingDataNote"];
export type BriefingUnjudged = S["BriefingUnjudged"];
export type BriefingGroup = S["BriefingGroup"];
export type BriefingCheck = S["BriefingCheck"];
export type BriefingView = "all" | "since_visit";
export type FeedbackRequest = S["FeedbackRequest"];
export type FeedbackResponse = S["FeedbackResponse"];
export type ItemFeedbackRequest = S["ItemFeedbackRequest"];
export type NoteFeedbackRequest = S["NoteFeedbackRequest"];
export type FeedbackEntry = S["FeedbackEntry"];
export type FeedbackList = S["FeedbackList"];
export type FeedbackStatus = S["FeedbackStatusRequest"]["status"];
export type FeedbackKind = FeedbackEntry["kind"];

/** Events on the POST /api/ask stream (`x-sse-events` in the OpenAPI document). */
export type AskEvent =
  | { event: "understanding"; data: Understanding }
  | { event: "status"; data: StatusEvent }
  | { event: "sources"; data: SourcesEvent }
  | { event: "verdict"; data: VerdictEvent }
  | { event: "charts"; data: ChartsEvent }
  | { event: "token"; data: TokenEvent }
  | { event: "answer"; data: Answer }
  | { event: "error"; data: ApiErrorBody };

export type Tone = "neutral" | "good" | "warn" | "bad" | "info";
export type Proposal = S["Proposal"];
export type AskDraft = S["AskDraft"];
export type ProposedChange = S["ProposedChange"];
export type DraftFigure = S["DraftFigure"];
export type Suggestions = S["Suggestions"];
export type ProactiveSuggestion = S["ProactiveSuggestion"];
export type SuggestionDraft = S["SuggestionDraft"];
export type SuggestionEta = S["SuggestionEta"];
export type CardActionOffer = S["CardActionOffer"];
export type ActionKind = CardActionOffer["kind"];
export type ActionPayload = S["ActionPayload"];
export type ActionRequest = S["ActionRequest"];
export type ActionConfirm = S["ActionConfirm"];
export type ActionPreview = S["ActionPreview"];
export type ActionResult = S["ActionResult"];
export type WriteIntentView = S["WriteIntentView"];
export type WriteIntentList = S["WriteIntentList"];
export type WritePreview = S["WritePreview"];
export type WriteApprove = S["WriteApprove"];
export type WriteExecute = S["WriteExecute"];
export type AlertPlaybook = S["AlertPlaybook"];
export type Brief = S["Brief"];
export type RecordPack = S["RecordPack"];
export type MeetingFollowUpResponse = S["MeetingFollowUp"];
export type MyActionItemsResponse = S["MyActionItems"];
export type ApprovalsQueue = S["ApprovalsQueue"];
export type AlertKinds = S["AlertKinds"];
export type AlertType = S["AlertType"];
export type ReplyTriage = S["ReplyTriage"];
export type ReplyLine = S["ReplyLine"];
export type MyWork = S["MyWork"];
export type MyWorkItem = S["MyWorkItem"];
export type AutopilotArea = S["AutopilotArea"];
export type AutopilotItem = S["AutopilotItem"];
export type AutopilotActionResult = S["AutopilotActionResult"];
export type AutopilotWorkflowRow = S["AutopilotWorkflowRow"];
export type AutopilotPerson = S["AutopilotPerson"];
export type AutopilotSwitches = S["AutopilotSwitches"];
export type AutopilotAudit = S["AutopilotAudit"];
export type AutopilotAuditRow = S["AutopilotAuditRow"];
export type AutopilotDecisionRequest = S["AutopilotDecisionRequest"];
export type AutopilotOptIn = S["AutopilotOptIn"];
export type AutopilotDraftChoice = S["AutopilotDraftChoice"];
export type AutopilotOptInPerson = S["AutopilotOptInPerson"];
export type AutopilotScoreBar = S["AutopilotScoreBar"];
export type AutopilotMode = S["AutopilotModeRequest"]["mode"];
export type AutopilotStep = S["AutopilotStep"];
export type AutopilotWorkflowDetail = S["AutopilotWorkflowDetail"];
export type AutopilotCatalog = S["AutopilotCatalog"];
export type AutopilotWorkflowDraft = S["AutopilotWorkflowDraft"];
export type AutopilotDraftKept = S["AutopilotDraftKept"];
export type AutopilotReplay = S["AutopilotReplay"];
export type AutopilotSchedules = S["AutopilotSchedules"];
export type AutopilotCountdown = S["AutopilotCountdown"];
export type AutopilotScorecard = S["AutopilotScorecard"];
export type AutopilotScoreDay = S["AutopilotScoreDay"];
export type TrustPage = S["TrustPage"];
export type FeedStates = S["FeedStates"];
export type FeedState = S["FeedState"];
export type TrustFreshness = S["TrustFreshness"];
export type TrustAnswerSet = S["TrustAnswerSet"];
export type TrustAnswerScore = S["TrustAnswerScore"];
export type TrustAlert = S["TrustAlert"];
export type TrustSwitch = S["TrustSwitch"];
export type InstructPart = S["InstructPart"];
export type InstructFill = S["InstructFill"];
export type FillField = S["FillField"];
export type DocFillRequest = S["DocFillRequest"];
export type InstructDocument = S["InstructDocument"];
export type InstructWrite = S["InstructWrite"];
export type EntryHold = S["EntryHold"];
export type EntryField = S["EntryField"];
export type EntryOption = S["EntryOption"];
export type EntryConfirm = S["EntryConfirm"];
export type UploadPreview = S["UploadPreview"];
export type PreparedWrite = S["PreparedWrite"];
export type TeamView = S["TeamView"];
export type Workload = S["Workload"];
export type WorkloadTeam = S["WorkloadTeam"];
export type WorkloadPerson = S["WorkloadPerson"];
export type WorkloadGrants = S["WorkloadGrants"];
export type WorkloadGrantSheet = S["WorkloadGrantSheet"];
export type WorkloadGrantPerson = S["WorkloadGrantPerson"];
export type TeamPerson = S["TeamPerson"];
export type TeamPersonDetail = S["TeamPersonDetail"];
export type TeamTrackerWeek = S["TeamTrackerWeek"];
export type TeamItem = S["TeamItem"];
export type LabelChanges = S["LabelChanges"];
export type LabelRevisions = S["LabelRevisions"];
export type LabelChange = S["LabelChange"];
export type BriefSection = S["BriefSection"];
export type BriefRow = S["BriefRow"];
export type BriefFigure = S["BriefFigure"];
export type UpcomingMeetings = S["UpcomingMeetings"];
export type UpcomingMeeting = S["UpcomingMeeting"];
export type MeetingInvitee = S["MeetingInvitee"];
export type MeetingOpenItem = S["MeetingOpenItem"];
export type ActionStates = S["ActionStates"];
export type ActionsLog = S["ActionsLog"];
export type ActionLogRow = S["ActionLogRow"];

export type Watchlist = S["Watchlist"];
export type WatchFollow = S["WatchFollow"];
export type WatchRule = S["WatchRule"];
export type WatchRuleDraft = S["WatchRuleDraft"];
export type WatchAlert = S["WatchAlert"];
export type WatchRuleDeleted = S["WatchRuleDeleted"];
export type WatchReadResult = S["WatchReadResult"];

export type WorkLogEvent = S["WorkLogEvent"];
export type WorkLogKind = WorkLogEvent["kind"];
export type WorkLogLoop = S["WorkLogLoop"];
export type WorkLogPage = S["WorkLogPage"];
export type TimeSaved = S["TimeSaved"];
export type ViewAsPeople = S["ViewAsPeople"];
export type TimeSavedPerson = S["TimeSavedPerson"];
export type WorkLogThread = S["WorkLogThread"];
export type WorkLogOrganise = S["WorkLogOrganise"];
export type WorkLogSave = S["WorkLogSave"];
export type AskActiveRun = S["AskActiveRun"];
export type AskRunStop = S["AskRunStop"];
export type AskChat = S["AskChat"];
export type AskChats = S["AskChats"];
export type AskChatUpdate = S["AskChatUpdate"];
export type MemoryItem = S["MemoryItem"];
export type MemoryItems = S["MemoryItems"];
export type InsightInfo = S["InsightInfo"];
/** The dashboard: the charts shown in the person's own order, the ones they took off (`hidden`),
 * and a said reason when their arrangement could not be read (the standard one is shown). */
export type InsightList = S["InsightList"] & { hidden?: InsightInfo[]; customised?: boolean; layout_problem?: string | null };
export type InsightResult = S["InsightResult"];
export type InsightSave = S["InsightSave"];
export type InsightSchedule = S["InsightSchedule"];
export type InsightNews = S["InsightNews"];
export type InsightNewsList = S["InsightNewsList"];
export type MemoryPeople = S["MemoryPeople"];
export type MemorySave = S["MemorySave"];
export type MemoryKind = MemorySave["kind"];
export type Template = S["Template"];
export type TemplateList = S["TemplateList"];
export type TemplateSave = S["TemplateSave"];
export type TemplateDocType = Template["doc_type"];
export type TemplateProposal = S["TemplateProposal"];
export type TemplateFilled = S["TemplateFilled"];
export type TemplateFillField = S["TemplateFillField"];
export type TemplateSuggestions = S["TemplateSuggestions"];
export type TemplateSuggestion = S["TemplateSuggestion"];

export type RecurringInstruction = S["RecurringInstruction"];
export type RecurringInstructions = S["RecurringInstructions"];
export type RecurringKeep = S["RecurringKeep"];
export type RecurringChange = S["RecurringChange"];

/** What Ask or Genie asked for that no view or measure defines (`GET /api/answers/gaps`). */
export interface GapRow { kind: "view" | "measure"; name: string; asked: number; first: string; last: string; defined: boolean }
export interface GapList { on: boolean; gaps: GapRow[] }

export type MeasureCandidate = S["MeasureCandidate"];
export type MeasureCandidateList = S["MeasureCandidateList"];
export type MeasureCandidatePropose = S["MeasureCandidatePropose"];
export type MeasureCandidateApprove = S["MeasureCandidateApprove"];
export type MeasureCandidateDismiss = S["MeasureCandidateDismiss"];
export type MeasureCandidateStatus = MeasureCandidate["status"];

/** How the Ask has been doing per day, from what each answer kept (`GET /api/answers/observed`). */
export interface AskDay { day: string; answered: number; failed: number; unavailable: number; median_ms: number | null; slowest_ms: number | null; median_steps: number | null; cost_usd?: string | null; cost_answers?: number }
export interface AskObserved { available: boolean; days: AskDay[]; failures: { at: string; question: string | null; error: string | null }[] }

/** Draft an email for a brief or answer with its chart (`POST /api/email-draft`). */
export interface EmailDraftRequest {
  title: string;
  summary?: string;
  to?: string | null;
  charts?: Record<string, unknown>[];
  citations?: Record<string, unknown>[];
  format?: "xlsx" | "docx" | "pptx" | "pdf";
  attach_file?: boolean;
}
export interface EmailDraftPrepared {
  card_id: string;
  kind: "send_email";
  subject: string;
  body: string;
  to: string | null;
  attachment_name: string | null;
  upload_id: string | null;
}

/** Export any chart, table or answer to Excel (`POST /api/export/xlsx`). */
export interface ExportTable {
  title?: string;
  columns: string[];
  rows: (string | null)[][];
  sql?: string | null;
  as_of?: string | null;
  total_rows?: number | null;
  records?: number | null;
  truncated?: boolean;
}
export interface ExportXlsxRequest {
  title?: string;
  summary?: string;
  tables?: ExportTable[];
  charts?: S["AskChart"][];
  citations?: Record<string, unknown>[];
}
