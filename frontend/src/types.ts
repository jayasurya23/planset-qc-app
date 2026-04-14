export type Status = 'Pass' | 'Fail' | 'Needs Review' | 'Overridden / Accepted by QC Engineer'

export interface CategorySummary {
  name: string
  total: number
  Pass?: number
  Fail?: number
  'Needs Review'?: number
  'Overridden / Accepted by QC Engineer'?: number
}

export interface Issue {
  id: string
  run_id: string
  category: string
  item_key: string
  title: string
  description: string
  severity: string
  status: Status
  auto_status: Status
  page_number?: number | null
  bbox?: { x0: number; y0: number; x1: number; y1: number } | null
  snippet_path?: string | null
  page_preview_path?: string | null
  evidence?: string | null
  confidence: number
  override_comment?: string | null
  created_at: string
  updated_at: string
}

export interface GeminiUsage {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  api_calls: number
}

export interface ProjectDetails {
  // ── Project Info ──
  project_name: string
  project_address: string
  site_coordinates: string
  county: string
  state: string
  parcel_id: string
  building_codes: string
  der_number: string

  // ── Owner / EPC / Engineering ──
  owner_name: string
  owner_address: string
  owner_phone: string
  epc_name: string
  epc_address: string
  epc_phone: string
  eor_name: string
  eor_license: string
  eor_state: string
  checker_name: string
  designer_name: string

  // ── System Info ──
  module_make: string
  module_model: string
  module_stc_watts: string
  module_voc: string
  module_vmp: string
  module_isc: string
  module_imp: string
  module_temp_coeff_voc: string
  module_temp_coeff_isc: string
  is_bifacial: string
  string_size: string
  string_quantity: string
  total_dc_kw: string
  inverter_make: string
  inverter_model: string
  inverter_kva: string
  inverter_kw: string
  inverter_max_vdc: string
  inverter_mppt_range: string
  inverter_quantity: string
  total_ac_kva: string
  dc_ac_ratio: string
  racking_make: string
  racking_model: string
  racking_type: string  // "fixed" | "tracker"
  pitch: string
  interrow_spacing: string
  gcr: string
  tilt_angle: string
  azimuth: string

  // ── Electrical ──
  poi_voltage: string
  feeder_grounding: string
  fault_current: string
  transformer_kva: string
  transformer_primary_voltage: string
  transformer_secondary_voltage: string
  transformer_winding_config: string
  transformer_impedance: string
  transformer_bil: string
  recloser_make: string
  recloser_continuous_a: string
  recloser_interrupting_ka: string
  ct_ratio: string
  vt_ratio: string
  meter_accuracy_class: string
  surge_arrestor_mcov: string

  // ── Utility ──
  utility_name: string
  utility_feeder: string
  is_ngrid: string
  ieee_category: string

  // ── Design Temps ──
  design_temp_low_c: string
  design_temp_high_c: string
  ambient_temp_c: string

  // ── Notes ──
  special_notes: string
}

export interface RunSummary {
  indexed_sheet_count: number
  actual_sheet_count: number
  pdf_page_count: number
  missing_from_pdf: string[]
  extra_sheets: string[]
  gemini_usage?: GeminiUsage
  page_sheet_map?: Record<string, string>
}

export interface RunData {
  id: string
  project_name: string
  original_filename: string
  created_at: string
  pdf_path: string
  page_count: number
  summary: RunSummary
  status_counts: Record<string, number>
  categories: CategorySummary[]
  issues: Issue[]
  project_details?: Partial<ProjectDetails> | null
}
