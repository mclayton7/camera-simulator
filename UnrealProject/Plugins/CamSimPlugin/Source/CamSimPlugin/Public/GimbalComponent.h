#pragma once

#include "CoreMinimal.h"
#include "Components/ActorComponent.h"
#include "GimbalComponent.generated.h"

/**
 * UGimbalComponent
 *
 * Models a two-axis (pan/tilt) stabilised gimbal.
 *
 * Pan  = rotation about the aircraft Z-axis (yaw), 0 = aircraft nose.
 *        Positive = clockwise / right when viewed from above.
 *        Hard limits: ±170°
 *
 * Tilt = rotation about the gimbal Y-axis (elevation).
 *        Positive = looking up; negative = looking down.
 *        Hard limits: −90° (straight down) to +30° (slightly above horizon)
 *
 * Commands from CommandReceiver arrive as rate inputs (deg/s) or absolute
 * positions.  Rate inputs are slew-rate limited and clamped to hard limits.
 */
UCLASS(ClassGroup = "CamSim", meta = (BlueprintSpawnableComponent))
class CAMSIMPLUGIN_API UGimbalComponent : public UActorComponent
{
    GENERATED_BODY()

public:
    UGimbalComponent();

    // -----------------------------------------------------------------------
    // Configuration
    // -----------------------------------------------------------------------

    /** Maximum slew rate in degrees/second */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "CamSim|Gimbal")
    float MaxSlewRateDegPerSec = 60.0f;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "CamSim|Gimbal")
    float PanLimitDeg = 170.0f;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "CamSim|Gimbal")
    float TiltMinDeg = -90.0f;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "CamSim|Gimbal")
    float TiltMaxDeg = 30.0f;

    // -----------------------------------------------------------------------
    // Current state
    // -----------------------------------------------------------------------

    UPROPERTY(BlueprintReadOnly, Category = "CamSim|Gimbal")
    float PanDeg = 0.0f;

    UPROPERTY(BlueprintReadOnly, Category = "CamSim|Gimbal")
    float TiltDeg = -45.0f;  // default: looking 45° below horizon

    // -----------------------------------------------------------------------
    // Commands (called from game thread)
    // -----------------------------------------------------------------------

    /** Apply pan and tilt rate commands (deg/s), applied each tick */
    void SetSlewRates(float PanRateDegPerSec, float TiltRateDegPerSec);

    /** Jump directly to an absolute pan/tilt position */
    void SetAbsolutePosition(float NewPanDeg, float NewTiltDeg);

    // -----------------------------------------------------------------------
    // Queries
    // -----------------------------------------------------------------------

    /** World-space rotation the camera should have based on current pan/tilt
     *  and the parent actor's heading.  Used by SimCameraComponent. */
    FRotator GetCameraWorldRotation() const;

    /** Sensor relative azimuth (0–360, wrapping) */
    float GetSensorRelAzDeg() const;

    /** Sensor relative elevation (signed) */
    float GetSensorRelElDeg() const;

    virtual void TickComponent(float DeltaTime, ELevelTick TickType,
                                FActorComponentTickFunction* ThisTickFunction) override;

protected:
    virtual void BeginPlay() override;

private:
    float PendingPanRate  = 0.0f;
    float PendingTiltRate = 0.0f;

    float ClampPan(float InDeg) const;
    float ClampTilt(float InDeg) const;
};
