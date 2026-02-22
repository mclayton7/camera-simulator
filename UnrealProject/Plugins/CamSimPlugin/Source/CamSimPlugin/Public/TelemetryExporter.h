#pragma once

#include "CoreMinimal.h"
#include "Components/ActorComponent.h"
#include "SharedMemoryTypes.h"
#include "TelemetryExporter.generated.h"

class AAircraftKinematicActor;
class UGimbalComponent;
class USimCameraComponent;
struct ShmTelemetryHeader;

/**
 * UTelemetryExporter
 *
 * Each game tick, collects flight + gimbal + camera state into a
 * TelemetryFrame and writes it to the shared-memory double-buffer so the
 * Python sidecar can build MISB ST 0601 KLV packets.
 *
 * Uses a seqlock pattern:
 *   1. Increment write_slot (0 → 1 → 0 → …)
 *   2. Write TelemetryFrame into the new slot.
 *   3. Write sequence (last field) to signal completion.
 * The sidecar reads from the slot that is NOT write_slot, checks sequence
 * before and after — if they match, the data is consistent.
 */
UCLASS(ClassGroup = "CamSim", meta = (BlueprintSpawnableComponent))
class CAMSIMPLUGIN_API UTelemetryExporter : public UActorComponent
{
    GENERATED_BODY()

public:
    UTelemetryExporter();
    virtual ~UTelemetryExporter();

    void SetSources(AAircraftKinematicActor* InAircraft,
                    UGimbalComponent*        InGimbal,
                    USimCameraComponent*     InCamera);

    virtual void TickComponent(float DeltaTime, ELevelTick TickType,
                                FActorComponentTickFunction* ThisTickFunction) override;

protected:
    virtual void BeginPlay() override;
    virtual void EndPlay(const EEndPlayReason::Type Reason) override;

private:
    UPROPERTY()
    TObjectPtr<AAircraftKinematicActor> Aircraft;

    UPROPERTY()
    TObjectPtr<UGimbalComponent> Gimbal;

    UPROPERTY()
    TObjectPtr<USimCameraComponent> Camera;

    // Shared memory
    void*    ShmPtr    = nullptr;
    size_t   ShmSize   = 0;
    uint32_t TelSeq    = 0;

#if PLATFORM_WINDOWS
    void*    ShmHandle = nullptr;
#else
    int      ShmFd     = -1;
#endif

    ShmTelemetryHeader* Header = nullptr;
    TelemetryFrame*     Slots  = nullptr;  // pointer to Slots[2] after header

    bool OpenSharedMemory();
    void CloseSharedMemory();

    void BuildAndWrite();
};
