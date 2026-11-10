type SessionWorker struct {
    Active       bool           // 1B + 7B hidden pad
    TenantID     uint64         // 8B
    Compression  uint8          // 1B + 7B hidden pad
    Endpoint     string         // 16B string header (ptr+len)
    RetryCount   uint16         // 2B + 6B hidden pad
    PayloadBuf   []byte         // 24B slice header (ptr+len+cap)
    WriteHits    atomic.Int64   // 8B @hot atomic counter
    ReadHits     atomic.Int64   // 8B @hot atomic counter
    noCopy       struct{}       // 0B trailing field forces +8B tail pad!
}
