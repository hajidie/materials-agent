export interface Attachment { attachment_id: string; kind: "dataset" | "ebsd_image" | "image" | "training_run" | "model" | "prediction"; name: string }
export interface Presentation {
  title: string; summary: string; facts: Record<string, unknown>;
  metrics: Array<{ label: string; value: number; unit?: string | null }>; notes: string[];
  unit_annotations?: Array<{ resource_parameter: string; column: string; unit: string; inferred_unit: string;
    source: "model_inference"; evidence: string; usage: "interpretation" | "numeric";
    provenance: "declared" | "confirmed" | "inferred"; conflict: boolean; requires_confirmation: boolean }>;
}
export interface ResultMessage { message_id: string; text: string; created_at: string; presentation: Presentation; artifacts: Attachment[] }
export interface ArtifactTarget { conversation: string; message: string; attachment: Attachment }
export interface ArtifactView extends Attachment { image_url?: string; presentation?: Presentation; downloads: Array<{ name: string; url: string }> }
