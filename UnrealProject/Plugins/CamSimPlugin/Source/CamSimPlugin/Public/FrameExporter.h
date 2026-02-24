#pragma once

#include "CoreMinimal.h"
#include "Components/ActorComponent.h"
#include "RHI.h"
#include "FrameExporter.generated.h"

class USimCameraComponent;
struct ShmFrameHeader;
struct ShmFrameSlot;

/**
 * UFrameExporter
 *
 * Each game tick, reads back the SceneCapture render target into CPU memory
 * (BGRA) and writes the frame into the shared-memory ring buffer so that the
 * Python sidecar can pick it up.
 *
 * Note: this currently uses ReadPixels() on the game thread (GPU flush).
 * At 30 fps this is acceptable for the simulator; keep allocations minimal.
 *
 * Shared memory layout: see SharedMemoryTypes.h
 */
UCLASS(ClassGroup = "CamSim", meta = (BlueprintSpawnableComponent))
class CAMSIMPLUGIN_API UFrameExporter : public UActorComponent
{
    GENERATED_BODY()

public:
    UFrameExporter();
    virtual ~UFrameExporter();

    void SetCameraComponent(USimCameraComponent* InCamera);

    virtual void TickComponent(float DeltaTime, ELevelTick TickType,
                                FActorComponentTickFunction* ThisTickFunction) override;

protected:
    virtual void BeginPlay() override;
    virtual void EndPlay(const EEndPlayReason::Type Reason) override;

private:
    UPROPERTY()
    TObjectPtr<USimCameraComponent> CameraComp;

    // Reused buffer to avoid per-tick allocations.
    TArray<FColor> PixelBuffer;

    // Shared memory
    void*    ShmPtr      = nullptr;
    size_t   ShmSize     = 0;
    uint32_t FrameSeq    = 0;

#if PLATFORM_WINDOWS
    void*    ShmHandle   = nullptr;
#else
    int      ShmFd       = -1;
#endif

    ShmFrameHeader* Header = nullptr;

    bool  OpenSharedMemory(uint32_t Width, uint32_t Height);
    void  CloseSharedMemory();

    /** Get pointer to slot N's ShmFrameSlot struct inside the ring */
    ShmFrameSlot* GetSlot(uint32_t SlotIndex);

    /** Copy pixels from render target into next ring-buffer slot */
    void WriteFrame(const TArray<FColor>& Pixels, uint32_t Width, uint32_t Height,
                    uint64_t TimestampUs);
};
