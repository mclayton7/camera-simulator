#pragma once

#include "CoreMinimal.h"
#include "Components/ActorComponent.h"
#include "Sockets.h"
#include "CommandReceiver.generated.h"

class AAircraftKinematicActor;
class UGimbalComponent;

// ---------------------------------------------------------------------------
// UDP Command Protocol — wire format
// Header: [magic u32 LE][msg_type u8][reserved u8][payload_len u16 LE]
// ---------------------------------------------------------------------------

static constexpr uint32_t CMD_MAGIC    = 0x43534D53u; // 'CSMS'
static constexpr uint32_t CMD_HDR_SIZE = 8u;

enum class ECamSimCmd : uint8
{
    SlewPan      = 0x01, // float pan_rate_deg_s
    SlewTilt     = 0x02, // float tilt_rate_deg_s
    SlewBoth     = 0x03, // float pan_rate_deg_s, float tilt_rate_deg_s
    SetPosition  = 0x04, // double lat_deg, double lon_deg, float alt_m_hae
    SetHeading   = 0x05, // float heading_deg
    SetSpeed     = 0x06, // float speed_kts
    SetGimbalAbs = 0x07, // float pan_deg, float tilt_deg
    Ping         = 0xFF, // no payload
};

/**
 * UCommandReceiver
 *
 * Binds a non-blocking UDP socket on port 5005 (configurable).
 * Polled at the top of each game tick; valid messages are decoded and
 * dispatched to AAircraftKinematicActor / UGimbalComponent on the game thread.
 *
 * Thread model: single-threaded (polled on game thread).  Max throughput is
 * ~30 Hz — adequate for manual slew commands.  If higher-rate external
 * controllers are needed, move to a dedicated receive thread + MPSC queue.
 */
UCLASS(ClassGroup = "CamSim", meta = (BlueprintSpawnableComponent))
class CAMSIMPLUGIN_API UCommandReceiver : public UActorComponent
{
    GENERATED_BODY()

public:
    UCommandReceiver();
    virtual ~UCommandReceiver();

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "CamSim|Network")
    int32 UDPPort = 5005;

    /** Wired by AircraftKinematicActor */
    void SetTargets(AAircraftKinematicActor* InAircraft, UGimbalComponent* InGimbal);

    virtual void TickComponent(float DeltaTime, ELevelTick TickType,
                                FActorComponentTickFunction* ThisTickFunction) override;

protected:
    virtual void BeginPlay() override;
    virtual void EndPlay(const EEndPlayReason::Type Reason) override;

private:
    FSocket* Socket = nullptr;

    UPROPERTY()
    TObjectPtr<AAircraftKinematicActor> AircraftTarget;

    UPROPERTY()
    TObjectPtr<UGimbalComponent> GimbalTarget;

    bool OpenSocket();
    void CloseSocket();

    /** Read and dispatch all pending datagrams (non-blocking) */
    void DrainSocket();

    /** Dispatch a single parsed command */
    void DispatchCommand(ECamSimCmd Type, const uint8* Payload, uint16 PayloadLen);
};
