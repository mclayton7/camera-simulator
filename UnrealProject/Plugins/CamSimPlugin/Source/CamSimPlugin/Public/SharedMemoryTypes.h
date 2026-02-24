#pragma once

#include "CoreMinimal.h"
#include <stdint.h>

// ---------------------------------------------------------------------------
// Shared Memory IPC wire contract — Unreal ↔ Python sidecar
//
// Two separate shm regions:
//   "camsim_frames"     — BGRA video frame ring buffer
//   "camsim_telemetry"  — double-buffered TelemetryFrame
//
// Both sides must agree on this layout exactly.  Compile with the same
// alignment rules: #pragma pack(push,1) / pack(pop) used throughout.
// ---------------------------------------------------------------------------

#pragma pack(push, 1)

// ===========================================================================
// Frame Ring Buffer (shm name: camsim_frames)
// ===========================================================================

static constexpr uint32_t CAMSIM_SHM_MAGIC      = 0x43534D46u; // 'CSMF'
static constexpr uint32_t CAMSIM_FRAME_SLOTS     = 3u;          // triple-buffer
static constexpr uint32_t CAMSIM_MAX_WIDTH       = 3840u;
static constexpr uint32_t CAMSIM_MAX_HEIGHT      = 2160u;
static constexpr uint32_t CAMSIM_BYTES_PER_PIXEL = 4u;          // BGRA

// Region layout:
//   [0]                : ShmFrameHeader  (64 bytes, cache-line aligned)
//   [sizeof(Header)]   : ShmFrameSlot[CAMSIM_FRAME_SLOTS]

struct ShmFrameHeader
{
    uint32_t magic;         // CAMSIM_SHM_MAGIC
    uint32_t version;       // 1
    uint32_t frame_width;
    uint32_t frame_height;
    uint32_t slot_count;    // CAMSIM_FRAME_SLOTS
    uint32_t slot_stride;   // bytes per slot (sizeof(ShmFrameSlot) padded to 4 k)

    // Producer (Unreal) increments write_index before writing a slot.
    // Consumer (sidecar) increments read_index after consuming a slot.
    // Both wrap mod slot_count.
    volatile uint32_t write_index;
    volatile uint32_t read_index;

    uint8_t  _pad[64 - 8 * sizeof(uint32_t)];
};
static_assert(sizeof(ShmFrameHeader) == 64, "ShmFrameHeader must be 64 bytes");

struct ShmFrameSlot
{
    uint32_t sequence;      // monotonically increasing frame counter
    uint32_t width;
    uint32_t height;
    uint8_t  _pad1[4];      // explicit pad: matches natural alignment of uint64_t in Python ctypes
    uint64_t timestamp_us;  // Unix epoch microseconds (UTC)
    uint32_t data_size;     // width * height * 4
    uint32_t _pad;

    // Pixel data follows immediately.  Declared as a flexible-array-member
    // equivalent — access via reinterpret_cast after the fixed header.
    // Total slot size = sizeof(ShmFrameSlot) + CAMSIM_MAX_WIDTH * CAMSIM_MAX_HEIGHT * 4
};
static_assert(sizeof(ShmFrameSlot) == 32, "ShmFrameSlot header must be 32 bytes");

// Convenience: total bytes per slot
inline constexpr size_t CamSimSlotPayloadSize(uint32_t w, uint32_t h)
{
    return sizeof(ShmFrameSlot) + static_cast<size_t>(w) * h * CAMSIM_BYTES_PER_PIXEL;
}

inline constexpr size_t CamSimFrameShmSize(uint32_t w, uint32_t h, uint32_t slots = CAMSIM_FRAME_SLOTS)
{
    return sizeof(ShmFrameHeader) + CamSimSlotPayloadSize(w, h) * slots;
}


// ===========================================================================
// Telemetry Double Buffer (shm name: camsim_telemetry)
// ===========================================================================

// Two TelemetryFrame slots side-by-side; ping-pong selected by write_slot.
// The sidecar always reads the slot that is NOT currently being written.
// Sequence is written last (acts as a seqlock — read it before & after).

struct TelemetryFrame
{
    uint64_t timestamp_us;              //  8  Unix epoch µs

    double   platform_lat_deg;          //  8  WGS-84 latitude
    double   platform_lon_deg;          //  8
    double   platform_alt_m_hae;        //  8  height above ellipsoid (m)

    float    platform_heading_deg;      //  4  0–360, true north
    float    platform_pitch_deg;        //  4  ±90
    float    platform_roll_deg;         //  4  ±180

    uint8_t  _pad1[4];                  //  4  explicit: matches double alignment gap in Python ctypes

    double   sensor_lat_deg;            //  8  sensor aperture WGS-84
    double   sensor_lon_deg;            //  8
    float    sensor_alt_m_hae;          //  4

    float    sensor_rel_az_deg;         //  4  pan relative to aircraft nose (0–360)
    float    sensor_rel_el_deg;         //  4  tilt (negative = looking down, ±180)
    float    sensor_rel_roll_deg;       //  4  sensor roll (0–360)

    float    hfov_deg;                  //  4  horizontal field-of-view
    float    vfov_deg;                  //  4  vertical field-of-view
    float    slant_range_m;             //  4  line-of-sight range to ground (m)

    uint8_t  _pad2[4];                  //  4  explicit: matches double alignment gap in Python ctypes

    double   frame_center_lat_deg;      //  8  image centre ground point
    double   frame_center_lon_deg;      //  8
    float    frame_center_elev_m;       //  4  ellipsoidal elevation of ground pt

    uint32_t sequence;                  //  4  written last; sidecar checks before+after

    uint8_t  _pad[8];                   //  8  explicit _pad + trailing struct alignment (matches Python 128-byte layout)
};
// Expected size: 8+8+8+8 + 4+4+4 + 4(_pad1) + 8+8+4 + 4+4+4+4+4+4 + 4(_pad2) + 8+8+4+4 + 8(_pad) = 128 bytes
static_assert(sizeof(TelemetryFrame) == 128, "TelemetryFrame must be 128 bytes");

struct ShmTelemetryHeader
{
    uint32_t magic;         // 0x43534D54 'CSMT'
    uint32_t version;       // 1
    volatile uint32_t write_slot;  // 0 or 1 — currently being written by Unreal
    uint32_t _pad;
    // Followed by TelemetryFrame[2]
};
static_assert(sizeof(ShmTelemetryHeader) == 16, "ShmTelemetryHeader must be 16 bytes");

inline constexpr size_t CamSimTelemetryShmSize()
{
    return sizeof(ShmTelemetryHeader) + 2 * sizeof(TelemetryFrame);
}

static constexpr uint32_t CAMSIM_TELEMETRY_MAGIC = 0x43534D54u; // 'CSMT'

#pragma pack(pop)
