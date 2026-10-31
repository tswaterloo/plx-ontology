// The app's names for operator_api's schemas. Every shape comes from the
// generated src/lib/openapi.d.ts (npm run gen:api), so a contract change
// shows up as a type error here, not as a blank screen.

import type { components } from "./openapi";

type S = components["schemas"];

export type Source = S["Source"];
export type SourceSystem = Source["system"];
export type SourceView = S["SourceView"];
export type Fact = S["Fact"];
export type NotFoundItem = S["NotFound"];
export type Hidden = S["Hidden"];
export type HiddenByOwner = S["HiddenOwner"];
export type Conflict = S["Conflict"];
export type ConflictValue = S["ConflictValue"];
export type Freshness = S["Freshness"];
export type ObjectRef = S["ObjectRef"];
export type Candidate = S["Candidate"];
export type UnderstoodEntity = S["UnderstoodEntity"];
export type Understanding = S["UnderstandingEvent"];
export type Intent = Understanding["intent"];
export type StatusEvent = S["StatusEvent"];
export type SourcesEvent = S["SourcesEvent"];
export type TokenEvent = S["TokenEvent"];
export type Answer = S["AnswerPayload"];
export type Profitability = S["Profitability"];
export type CostBreakdownView = S["CostBreakdown"];
export type CostLine = S["CostLine"];
export type CostSource = S["CostSource"];
export type OrderMargin = S["OrderMargin"];
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
export type HomeResponse = S["Home"];
export type NeedsAttention = S["NeedsAttention"];
export type LatePo = S["LatePo"];
export type AtRiskItem = S["AtRisk"];
export type RecentQuestion = S["RecentQuestion"];
export type ObjectPage = S["ObjectPage"];
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
export type NextAction = S["NextAction"];
export type MaterialRow = S["MaterialRow"];
export type TrailGap = S["TrailGap"];
export type FeedbackRequest = S["FeedbackRequest"];
export type FeedbackResponse = S["FeedbackResponse"];

/** Events on the POST /api/ask stream (`x-sse-events` in the OpenAPI document). */
export type AskEvent =
  | { event: "understanding"; data: Understanding }
  | { event: "status"; data: StatusEvent }
  | { event: "sources"; data: SourcesEvent }
  | { event: "token"; data: TokenEvent }
  | { event: "answer"; data: Answer }
  | { event: "error"; data: ApiErrorBody };

export type Tone = "neutral" | "good" | "warn" | "bad" | "info";
