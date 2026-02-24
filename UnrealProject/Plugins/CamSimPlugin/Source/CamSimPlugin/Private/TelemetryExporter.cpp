#include "TelemetryExporter.h"
#include "AircraftKinematicActor.h"
#include "GimbalComponent.h"
#include "SimCameraComponent.h"
#include "SharedMemoryTypes.h"

#include "HAL/PlatformMisc.h"
#include "Misc/DateTime.h"

#if PLATFORM_WINDOWS
#include "Windows/WindowsHWrapper.h"
#else
#include <sys/mman.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#endif

static constexpr const TCHAR* SHM_TEL_NAME = TEXT("camsim_telemetry");

UTelemetryExporter::UTelemetryExporter()
{
    PrimaryComponentTick.bCanEverTick = true;
    PrimaryComponentTick.TickGroup    = TG_PostUpdateWork;
}

UTelemetryExporter::~UTelemetryExporter()
{
    CloseSharedMemory();
}

void UTelemetryExporter::SetSources(AAircraftKinematicActor* InAircraft,
                                     UGimbalComponent*        InGimbal,
                                     USimCameraComponent*     InCamera)
{
    Aircraft = InAircraft;
    Gimbal   = InGimbal;
    Camera   = InCamera;
}

void UTelemetryExporter::BeginPlay()
{
    Super::BeginPlay();
    OpenSharedMemory();
}

void UTelemetryExporter::EndPlay(const EEndPlayReason::Type Reason)
{
    CloseSharedMemory();
    Super::EndPlay(Reason);
}

void UTelemetryExporter::TickComponent(float DeltaTime, ELevelTick TickType,
                                        FActorComponentTickFunction* ThisTickFunction)
{
    Super::TickComponent(DeltaTime, TickType, ThisTickFunction);
    if (Header) BuildAndWrite();
}

// ---------------------------------------------------------------------------
// Shared memory
// ---------------------------------------------------------------------------

bool UTelemetryExporter::OpenSharedMemory()
{
    ShmSize = CamSimTelemetryShmSize();

#if PLATFORM_WINDOWS
    HANDLE Handle = CreateFileMappingA(
        INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE,
        0, static_cast<DWORD>(ShmSize),
        TCHAR_TO_ANSI(SHM_TEL_NAME));
    if (!Handle) return false;
    ShmHandle = Handle;
    ShmPtr    = MapViewOfFile(Handle, FILE_MAP_ALL_ACCESS, 0, 0, ShmSize);
    if (!ShmPtr) { CloseHandle(Handle); ShmHandle = nullptr; return false; }
#else
    const std::string ShmPath = "/" + std::string(TCHAR_TO_UTF8(SHM_TEL_NAME));
    ShmFd = shm_open(ShmPath.c_str(), O_CREAT | O_RDWR, 0666);
    if (ShmFd < 0) return false;
    if (ftruncate(ShmFd, static_cast<off_t>(ShmSize)) != 0)
    {
        close(ShmFd); ShmFd = -1; return false;
    }
    ShmPtr = mmap(nullptr, ShmSize, PROT_READ | PROT_WRITE, MAP_SHARED, ShmFd, 0);
    if (ShmPtr == MAP_FAILED) { close(ShmFd); ShmFd = -1; ShmPtr = nullptr; return false; }
#endif

    FMemory::Memzero(ShmPtr, ShmSize);
    Header = reinterpret_cast<ShmTelemetryHeader*>(ShmPtr);
    Header->magic   = CAMSIM_TELEMETRY_MAGIC;
    Header->version = 1;
    Header->write_slot = 0;
    Slots = reinterpret_cast<TelemetryFrame*>(
        reinterpret_cast<uint8*>(ShmPtr) + sizeof(ShmTelemetryHeader));

    UE_LOG(LogTemp, Log, TEXT("CamSim TelemetryExporter: shm opened, %zu bytes"), ShmSize);
    return true;
}

void UTelemetryExporter::CloseSharedMemory()
{
    if (!ShmPtr) return;
#if PLATFORM_WINDOWS
    UnmapViewOfFile(ShmPtr);
    CloseHandle(ShmHandle);
    ShmHandle = nullptr;
#else
    munmap(ShmPtr, ShmSize);
    close(ShmFd);
    ShmFd = -1;
    shm_unlink(TCHAR_TO_UTF8(SHM_TEL_NAME));
#endif
    ShmPtr = nullptr;
    Header = nullptr;
    Slots  = nullptr;
}

// ---------------------------------------------------------------------------
// Build + write telemetry (seqlock write pattern)
// ---------------------------------------------------------------------------

void UTelemetryExporter::BuildAndWrite()
{
    if (!Aircraft || !Gimbal || !Camera) return;

    // Toggle write slot (0 → 1 → 0 …)
    const uint32_t WriteSlot = 1u - Header->write_slot;
    Header->write_slot = WriteSlot;

    TelemetryFrame& F = Slots[WriteSlot];

    const int64 NowUs      = FDateTime::UtcNow().GetTicks() / 10;
    F.timestamp_us         = static_cast<uint64_t>(NowUs);

    F.platform_lat_deg     = Aircraft->CurrentLatDeg;
    F.platform_lon_deg     = Aircraft->CurrentLonDeg;
    F.platform_alt_m_hae   = Aircraft->CurrentAltMHAE;
    F.platform_heading_deg = Aircraft->CurrentHeadingDeg;
    F.platform_pitch_deg   = Aircraft->PlatformPitchDeg;
    F.platform_roll_deg    = Aircraft->PlatformRollDeg;

    // Sensor position = aircraft position (gimbal is co-located)
    F.sensor_lat_deg       = Aircraft->CurrentLatDeg;
    F.sensor_lon_deg       = Aircraft->CurrentLonDeg;
    F.sensor_alt_m_hae     = static_cast<float>(Aircraft->CurrentAltMHAE);

    F.sensor_rel_az_deg    = Gimbal->GetSensorRelAzDeg();
    F.sensor_rel_el_deg    = Gimbal->GetSensorRelElDeg();
    F.sensor_rel_roll_deg  = 0.0f;  // assume stabilised roll

    F.hfov_deg             = Camera->HFovDeg;
    F.vfov_deg             = Camera->GetVFovDeg();
    F.slant_range_m        = Camera->SlantRangeM;

    F.frame_center_lat_deg = Camera->FrameCenterLatDeg;
    F.frame_center_lon_deg = Camera->FrameCenterLonDeg;
    F.frame_center_elev_m  = Camera->FrameCenterElevM;

    // Write sequence last — this is the "unlock" in the seqlock
    FPlatformMisc::MemoryBarrier();
    F.sequence = ++TelSeq;
}
