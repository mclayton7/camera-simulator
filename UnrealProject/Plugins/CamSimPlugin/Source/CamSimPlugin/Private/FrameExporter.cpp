#include "FrameExporter.h"
#include "SimCameraComponent.h"
#include "SharedMemoryTypes.h"

#include "Engine/TextureRenderTarget2D.h"
#include "RenderingThread.h"
#include "RHICommandList.h"
#include "Misc/DateTime.h"

#if PLATFORM_WINDOWS
#include "Windows/WindowsHWrapper.h"
#else
#include <sys/mman.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#endif

static const FString SHM_FRAME_NAME = TEXT("camsim_frames");

UFrameExporter::UFrameExporter()
{
    PrimaryComponentTick.bCanEverTick = true;
    PrimaryComponentTick.TickGroup    = TG_PostUpdateWork;
}

UFrameExporter::~UFrameExporter()
{
    CloseSharedMemory();
}

void UFrameExporter::SetCameraComponent(USimCameraComponent* InCamera)
{
    CameraComp = InCamera;
}

void UFrameExporter::BeginPlay()
{
    Super::BeginPlay();
    // Shared memory opened on first frame (dimensions known after BeginPlay)
}

void UFrameExporter::EndPlay(const EEndPlayReason::Type Reason)
{
    CloseSharedMemory();
    Super::EndPlay(Reason);
}

// ---------------------------------------------------------------------------
// Tick — read back last capture, write to shm
// ---------------------------------------------------------------------------

void UFrameExporter::TickComponent(float DeltaTime, ELevelTick TickType,
                                    FActorComponentTickFunction* ThisTickFunction)
{
    Super::TickComponent(DeltaTime, TickType, ThisTickFunction);

    if (!CameraComp) return;
    UTextureRenderTarget2D* RT = CameraComp->TextureTarget;
    if (!RT) return;

    const uint32_t W = static_cast<uint32_t>(RT->SizeX);
    const uint32_t H = static_cast<uint32_t>(RT->SizeY);

    // Open shm once dimensions are known
    if (!ShmPtr)
    {
        if (!OpenSharedMemory(W, H)) return;
    }

    // Read pixels from render target (blocks until GPU flush — acceptable at 30 fps)
    TArray<FColor> Pixels;
    FRenderTarget* RenderTarget = RT->GameThread_GetRenderTargetResource();
    if (!RenderTarget) return;

    if (!RenderTarget->ReadPixels(Pixels))
    {
        UE_LOG(LogTemp, Warning, TEXT("CamSim FrameExporter: ReadPixels failed"));
        return;
    }

    // Timestamp: Unix epoch in microseconds
    const int64 NowUs = FDateTime::UtcNow().GetTicks() / 10; // 100ns → µs

    WriteFrame(Pixels, W, H, static_cast<uint64_t>(NowUs));
}

// ---------------------------------------------------------------------------
// Shared memory
// ---------------------------------------------------------------------------

bool UFrameExporter::OpenSharedMemory(uint32_t Width, uint32_t Height)
{
    ShmSize = CamSimFrameShmSize(Width, Height);

#if PLATFORM_WINDOWS
    HANDLE Handle = CreateFileMappingA(
        INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE,
        (DWORD)(ShmSize >> 32), (DWORD)(ShmSize & 0xFFFFFFFF),
        TCHAR_TO_ANSI(*SHM_FRAME_NAME));
    if (!Handle)
    {
        UE_LOG(LogTemp, Error, TEXT("CamSim FrameExporter: CreateFileMapping failed (%d)"), GetLastError());
        return false;
    }
    ShmHandle = Handle;
    ShmPtr = MapViewOfFile(Handle, FILE_MAP_ALL_ACCESS, 0, 0, ShmSize);
    if (!ShmPtr)
    {
        UE_LOG(LogTemp, Error, TEXT("CamSim FrameExporter: MapViewOfFile failed (%d)"), GetLastError());
        CloseHandle(Handle);
        ShmHandle = nullptr;
        return false;
    }
#else
    const std::string ShmNameUtf8 = TCHAR_TO_UTF8(*SHM_FRAME_NAME);
    const std::string ShmPath     = "/" + ShmNameUtf8;

    ShmFd = shm_open(ShmPath.c_str(), O_CREAT | O_RDWR, 0666);
    if (ShmFd < 0)
    {
        UE_LOG(LogTemp, Error, TEXT("CamSim FrameExporter: shm_open failed errno=%d"), errno);
        return false;
    }
    if (ftruncate(ShmFd, static_cast<off_t>(ShmSize)) != 0)
    {
        UE_LOG(LogTemp, Error, TEXT("CamSim FrameExporter: ftruncate failed errno=%d"), errno);
        close(ShmFd); ShmFd = -1;
        return false;
    }
    ShmPtr = mmap(nullptr, ShmSize, PROT_READ | PROT_WRITE, MAP_SHARED, ShmFd, 0);
    if (ShmPtr == MAP_FAILED)
    {
        UE_LOG(LogTemp, Error, TEXT("CamSim FrameExporter: mmap failed errno=%d"), errno);
        close(ShmFd); ShmFd = -1; ShmPtr = nullptr;
        return false;
    }
#endif

    // Initialise header
    FMemory::Memzero(ShmPtr, sizeof(ShmFrameHeader));
    Header = reinterpret_cast<ShmFrameHeader*>(ShmPtr);
    Header->magic        = CAMSIM_SHM_MAGIC;
    Header->version      = 1;
    Header->frame_width  = Width;
    Header->frame_height = Height;
    Header->slot_count   = CAMSIM_FRAME_SLOTS;
    Header->slot_stride  = static_cast<uint32_t>(CamSimSlotPayloadSize(Width, Height));
    Header->write_index  = 0;
    Header->read_index   = 0;

    UE_LOG(LogTemp, Log, TEXT("CamSim FrameExporter: shm opened, %ux%u, %zu bytes"),
           Width, Height, ShmSize);
    return true;
}

void UFrameExporter::CloseSharedMemory()
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
    // Unlink so next run starts fresh
    shm_unlink(TCHAR_TO_UTF8(*SHM_FRAME_NAME));
#endif
    ShmPtr = nullptr;
    Header = nullptr;
}

ShmFrameSlot* UFrameExporter::GetSlot(uint32_t SlotIndex)
{
    check(Header);
    uint8* Base = reinterpret_cast<uint8*>(ShmPtr) + sizeof(ShmFrameHeader);
    return reinterpret_cast<ShmFrameSlot*>(Base + SlotIndex * Header->slot_stride);
}

void UFrameExporter::WriteFrame(const TArray<FColor>& Pixels, uint32_t Width, uint32_t Height,
                                 uint64_t TimestampUs)
{
    if (!Header) return;

    const uint32_t SlotIdx = Header->write_index % CAMSIM_FRAME_SLOTS;
    ShmFrameSlot*  Slot    = GetSlot(SlotIdx);

    Slot->sequence     = ++FrameSeq;
    Slot->width        = Width;
    Slot->height       = Height;
    Slot->timestamp_us = TimestampUs;
    Slot->data_size    = Width * Height * CAMSIM_BYTES_PER_PIXEL;

    // Copy BGRA pixels — FColor is BGRA on little-endian platforms
    uint8* Dst = reinterpret_cast<uint8*>(Slot) + sizeof(ShmFrameSlot);
    FMemory::Memcpy(Dst, Pixels.GetData(), static_cast<size_t>(Slot->data_size));

    // Advance write index (atomic-store equivalent via volatile)
    FPlatformAtomics::InterlockedIncrement(reinterpret_cast<volatile int32*>(&Header->write_index));
}
