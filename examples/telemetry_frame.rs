#[repr(C)]
pub struct TelemetryFrame {
    pub version: u8,             // 1B
    pub trace_id: u128,          // 16B (requires 16B alignment!)
    pub sampled: bool,           // 1B
    pub span_id: u64,            // 8B
    pub status_code: u16,        // 2B
    pub service_name: String,    // 24B (align 8)
    pub cpu_cycles: AtomicU64,   // 8B @hot
    pub drop_count: AtomicU64,   // 8B @hot
    pub region_code: [u8; 3],    // 3B
}
