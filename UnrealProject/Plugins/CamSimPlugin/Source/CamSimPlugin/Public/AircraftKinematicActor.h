#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "AircraftKinematicActor.generated.h"

class UCesiumGeoreference;
class UGimbalComponent;
class USimCameraComponent;
class UCommandReceiver;
class UFrameExporter;
class UTelemetryExporter;

/**
 * AircraftKinematicActor
 *
 * Represents a fixed-wing aircraft flying over Cesium World Terrain.
 * Each tick the actor:
 *   1. Advances lat/lon using heading + speed (haversine dead-reckoning).
 *   2. Updates its Unreal world transform via ACesiumGeoreference.
 *   3. Delegates gimbal slew and camera capture to sub-components.
 *
 * Initial position, heading, altitude, and speed are set via properties or
 * UDP commands (SetPosition, SetHeading, SetSpeed).
 */
UCLASS(Blueprintable, BlueprintType, ClassGroup = "CamSim")
class CAMSIMPLUGIN_API AAircraftKinematicActor : public AActor
{
    GENERATED_BODY()

public:
    AAircraftKinematicActor();

    // -----------------------------------------------------------------------
    // Initial flight state (editable in editor / overridable via UDP)
    // -----------------------------------------------------------------------

    /** Starting latitude (WGS-84 degrees) */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "CamSim|FlightState")
    double InitialLatDeg = 36.5;

    /** Starting longitude (WGS-84 degrees) */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "CamSim|FlightState")
    double InitialLonDeg = -117.5;

    /** Starting altitude above WGS-84 ellipsoid (metres) */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "CamSim|FlightState")
    double InitialAltMHAE = 1500.0;

    /** Starting true heading (degrees, 0 = north, clockwise) */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "CamSim|FlightState")
    float InitialHeadingDeg = 0.0f;

    /** Airspeed (knots) */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "CamSim|FlightState")
    float SpeedKts = 120.0f;

    /** Cruise pitch for visual model (degrees, positive = nose up) */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "CamSim|FlightState")
    float PlatformPitchDeg = 2.0f;

    /** Bank angle (degrees, positive = right wing down) */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "CamSim|FlightState")
    float PlatformRollDeg = 0.0f;

    // -----------------------------------------------------------------------
    // Runtime state (read-only from Blueprint)
    // -----------------------------------------------------------------------

    UPROPERTY(BlueprintReadOnly, Category = "CamSim|FlightState")
    double CurrentLatDeg;

    UPROPERTY(BlueprintReadOnly, Category = "CamSim|FlightState")
    double CurrentLonDeg;

    UPROPERTY(BlueprintReadOnly, Category = "CamSim|FlightState")
    double CurrentAltMHAE;

    UPROPERTY(BlueprintReadOnly, Category = "CamSim|FlightState")
    float CurrentHeadingDeg;

    // -----------------------------------------------------------------------
    // Sub-components
    // -----------------------------------------------------------------------

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "CamSim")
    TObjectPtr<UGimbalComponent> GimbalComponent;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "CamSim")
    TObjectPtr<USimCameraComponent> CameraComponent;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "CamSim")
    TObjectPtr<UCommandReceiver> CommandReceiverComponent;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "CamSim")
    TObjectPtr<UFrameExporter> FrameExporterComponent;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "CamSim")
    TObjectPtr<UTelemetryExporter> TelemetryExporterComponent;

    // -----------------------------------------------------------------------
    // UDP command handlers (called from CommandReceiver on game thread)
    // -----------------------------------------------------------------------

    void HandleSetPosition(double LatDeg, double LonDeg, float AltMHAE);
    void HandleSetHeading(float HeadingDeg);
    void HandleSetSpeed(float SpeedKts);
    void HandleSetFlightState(double LatDeg, double LonDeg, float AltMHAE,
                              float HeadingDeg, float PitchDeg, float RollDeg,
                              float SpeedKts);

protected:
    virtual void BeginPlay() override;
    virtual void Tick(float DeltaTime) override;

private:
    /** Cached reference to the CesiumGeoreference actor in the level */
    UPROPERTY()
    TObjectPtr<ACesiumGeoreference> CesiumGeoreference;

    /** When true, AdvancePosition() is skipped — an external flight director owns position. */
    bool bExternallyDriven = false;

    /** Advance lat/lon by DeltaTime seconds at current heading + speed */
    void AdvancePosition(float DeltaTime);

    /** Push new lat/lon/alt/heading to Unreal world transform */
    void SyncWorldTransform();
};
