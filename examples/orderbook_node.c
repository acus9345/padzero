struct OrderTickNode {
    bool is_aggressive;          // 1B flag
    uint64_t order_id;           // 8B identifier
    uint8_t venue_id;            // 1B exchange code
    double limit_price;          // 8B IEEE-754 price
    char side_flag;              // 1B 'B' or 'S'
    uint64_t match_ns;           // 8B nanosecond timestamp
    uint16_t flags;              // 2B bitfield
    void *next_order;            // 8B pointer
    uint8_t shard_tag[3];        // 3B routing tag
    atomic_ullong producer_seq;  // 8B @hot producer cursor
    atomic_ullong consumer_seq;  // 8B @hot consumer cursor
};
