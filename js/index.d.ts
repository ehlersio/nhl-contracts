export const BASE_URL: string;
export const TABLES: readonly string[];
export function fileUrl(name: string): string;
export interface LoadOptions { fetch?: typeof fetch }
export interface Timestamp {
  updated_at: string; version: string; n_contracts: number; n_claims: number; n_sources: number;
  n_conflicts: number; n_contract_seasons: number; n_clauses: number; n_dead_cap?: number;
  tiers: Record<string, number>; files: string[]; license: { data: string; code: string };
}
export interface Contract {
  contract_key: string; player_name: string; player_id: number | null; team: string | null;
  signed_at: string; signing_date_stated: boolean; contract_type: "ELC" | "standard" | "extension" | "PTO";
  term_years: number | null; aav_usd: number | null; cap_hit_usd: number | null; total_usd: number | null;
  nhl_salary_usd: number | null; minors_salary_usd: number | null; one_way: boolean | null;
  start_season: string | null; end_season: string | null; expiry_status: string | null; agent: string | null;
  re_signed: boolean; conflicts: string; n_sources: number; best_tier: string; tiers: string;
  source_ids: string; claim_ids: string; flags: string;
}
export interface ContractSeason {
  contract_key: string; season: string; cap_hit_usd: number | null; aav_usd: number | null;
  base_salary_usd: number | null; signing_bonus_usd: number | null; performance_bonus_usd: number | null;
  total_salary_usd: number | null; minors_salary_usd: number | null; one_way: boolean | null;
  clause: string | null; conflicts: string; n_sources: number; best_tier: string; source_ids: string;
}
export interface Clause {
  contract_key: string; kind: "NMC" | "NTC" | "M-NTC"; season_from: string | null; season_to: string | null;
  list_size: number | null; list_type: string | null; conflicts: string; n_sources: number; best_tier: string;
  source_ids: string;
}
export function timestamp(options?: LoadOptions): Promise<Timestamp>;
export interface DeadCap {
  team: string | null; kind: "buyout" | "retained"; player_name: string; player_id: number | null;
  counterparty: string | null; season: string; cap_hit_usd: number | null; pct: number | null;
  contract_key: string | null; event_date: string | null; conflicts: string; n_sources: number;
  best_tier: string; source_ids: string; flags: string;
}
export function load(table: "contracts", options?: LoadOptions): Promise<Contract[]>;
export function load(table: "contract_seasons", options?: LoadOptions): Promise<ContractSeason[]>;
export function load(table: "clauses", options?: LoadOptions): Promise<Clause[]>;
export function load(table: "dead_cap", options?: LoadOptions): Promise<DeadCap[]>;
export function load(table: string, options?: LoadOptions): Promise<Record<string, unknown>[]>;
export function parseCsv(text: string): Record<string, string>[];
