#include "GimbalComponent.h"
#include "GameFramework/Actor.h"

UGimbalComponent::UGimbalComponent()
{
    PrimaryComponentTick.bCanEverTick = true;
}

void UGimbalComponent::BeginPlay()
{
    Super::BeginPlay();
}

// ---------------------------------------------------------------------------
// Command interface
// ---------------------------------------------------------------------------

void UGimbalComponent::SetSlewRates(float PanRateDegPerSec, float TiltRateDegPerSec)
{
    // Clamp individual rates to ±MaxSlewRateDegPerSec
    PendingPanRate  = FMath::Clamp(PanRateDegPerSec,  -MaxSlewRateDegPerSec, MaxSlewRateDegPerSec);
    PendingTiltRate = FMath::Clamp(TiltRateDegPerSec, -MaxSlewRateDegPerSec, MaxSlewRateDegPerSec);
}

void UGimbalComponent::SetAbsolutePosition(float NewPanDeg, float NewTiltDeg)
{
    PanDeg  = ClampPan(NewPanDeg);
    TiltDeg = ClampTilt(NewTiltDeg);
    // Clear any pending slew
    PendingPanRate  = 0.0f;
    PendingTiltRate = 0.0f;
}

// ---------------------------------------------------------------------------
// Tick — integrate rates, apply limits
// ---------------------------------------------------------------------------

void UGimbalComponent::TickComponent(float DeltaTime, ELevelTick TickType,
                                      FActorComponentTickFunction* ThisTickFunction)
{
    Super::TickComponent(DeltaTime, TickType, ThisTickFunction);

    if (FMath::Abs(PendingPanRate) > KINDA_SMALL_NUMBER)
    {
        PanDeg = ClampPan(PanDeg + PendingPanRate * DeltaTime);
    }
    if (FMath::Abs(PendingTiltRate) > KINDA_SMALL_NUMBER)
    {
        TiltDeg = ClampTilt(TiltDeg + PendingTiltRate * DeltaTime);
    }
}

// ---------------------------------------------------------------------------
// Queries
// ---------------------------------------------------------------------------

FRotator UGimbalComponent::GetCameraWorldRotation() const
{
    const AActor* Owner = GetOwner();
    if (!Owner) return FRotator::ZeroRotator;

    // Start with aircraft orientation
    FRotator AircraftRot = Owner->GetActorRotation();

    // Apply gimbal pan (yaw relative to aircraft nose) then tilt (pitch)
    FRotator GimbalLocal(TiltDeg, PanDeg, 0.0f);

    // Combine: rotate the local gimbal offset by the aircraft world rotation
    FQuat WorldQuat = AircraftRot.Quaternion() * GimbalLocal.Quaternion();
    return WorldQuat.Rotator();
}

float UGimbalComponent::GetSensorRelAzDeg() const
{
    // Wrap to [0, 360)
    float Az = FMath::Fmod(PanDeg, 360.0f);
    if (Az < 0.0f) Az += 360.0f;
    return Az;
}

float UGimbalComponent::GetSensorRelElDeg() const
{
    return TiltDeg;
}

// ---------------------------------------------------------------------------
// Private helpers
// ---------------------------------------------------------------------------

float UGimbalComponent::ClampPan(float InDeg) const
{
    return FMath::Clamp(InDeg, -PanLimitDeg, PanLimitDeg);
}

float UGimbalComponent::ClampTilt(float InDeg) const
{
    return FMath::Clamp(InDeg, TiltMinDeg, TiltMaxDeg);
}
