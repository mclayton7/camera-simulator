#include "SimCameraComponent.h"
#include "GimbalComponent.h"
#include "Engine/TextureRenderTarget2D.h"
#include "Engine/World.h"
#include "DrawDebugHelpers.h"

#include "CesiumGeoreference.h"

USimCameraComponent::USimCameraComponent()
{
    PrimaryComponentTick.bCanEverTick = true;
    PrimaryComponentTick.TickGroup    = TG_PostUpdateWork;  // after physics/transforms

    // SceneCapture settings
    CaptureSource = ESceneCaptureSource::SCS_FinalColorLDR;
    bCaptureEveryFrame = true;
    bCaptureOnMovement = false;
    ShowFlags.SetTemporalAA(false);
    ShowFlags.SetMotionBlur(false);
    ShowFlags.SetBloom(false);
}

void USimCameraComponent::BeginPlay()
{
    Super::BeginPlay();
    EnsureRenderTarget();
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

void USimCameraComponent::SetGimbalComponent(UGimbalComponent* InGimbal)
{
    GimbalComp = InGimbal;
}

void USimCameraComponent::SetCesiumGeoreference(ACesiumGeoreference* InRef)
{
    CesiumRef = InRef;
}

// ---------------------------------------------------------------------------
// Tick
// ---------------------------------------------------------------------------

void USimCameraComponent::TickComponent(float DeltaTime, ELevelTick TickType,
                                         FActorComponentTickFunction* ThisTickFunction)
{
    Super::TickComponent(DeltaTime, TickType, ThisTickFunction);

    if (GimbalComp)
    {
        SetWorldRotation(GimbalComp->GetCameraWorldRotation());
    }

    UpdateGroundPoint();
}

// ---------------------------------------------------------------------------
// FoV
// ---------------------------------------------------------------------------

float USimCameraComponent::GetVFovDeg() const
{
    // Derive VFoV from HFoV assuming 16:9 aspect
    const float AspectRatio = static_cast<float>(CaptureWidth) / static_cast<float>(CaptureHeight);
    // VFoV = 2 * atan(tan(HFoV/2) / AspectRatio)
    const float HalfHFovRad = FMath::DegreesToRadians(HFovDeg * 0.5f);
    return FMath::RadiansToDegrees(2.0f * FMath::Atan(FMath::Tan(HalfHFovRad) / AspectRatio));
}

// ---------------------------------------------------------------------------
// Private helpers
// ---------------------------------------------------------------------------

void USimCameraComponent::UpdateGroundPoint()
{
    if (!GetWorld()) return;

    // Ray from camera in its look direction
    const FVector Origin    = GetComponentLocation();
    const FVector Direction = GetForwardVector();
    const FVector End       = Origin + Direction * 50000.0f * 100.0f; // 50 km in cm

    FHitResult HitResult;
    FCollisionQueryParams QueryParams;
    QueryParams.AddIgnoredActor(GetOwner());

    bool bHit = GetWorld()->LineTraceSingleByChannel(HitResult, Origin, End,
                                                      ECC_Visibility, QueryParams);
    if (bHit)
    {
        // Slant range in metres (Unreal units are cm)
        SlantRangeM = HitResult.Distance / 100.0f;

        // Convert hit location to geodetic if we have a Cesium reference
        if (CesiumRef)
        {
            FVector LLH = CesiumRef->TransformUnrealPositionToLongitudeLatitudeHeight(HitResult.Location);
            FrameCenterLonDeg  = LLH.X;
            FrameCenterLatDeg  = LLH.Y;
            FrameCenterElevM   = static_cast<float>(LLH.Z);
        }
    }
    else
    {
        // No hit — report a large slant range
        SlantRangeM = 50000.0f;
    }

    // Apply the FoV to the SceneCapture
    FOVAngle = HFovDeg;
}

void USimCameraComponent::EnsureRenderTarget()
{
    if (TextureTarget &&
        TextureTarget->SizeX == CaptureWidth &&
        TextureTarget->SizeY == CaptureHeight)
    {
        return;
    }

    TextureTarget = NewObject<UTextureRenderTarget2D>(this, TEXT("SimCameraRT"));
    TextureTarget->RenderTargetFormat = RTF_RGBA8;
    TextureTarget->InitAutoFormat(CaptureWidth, CaptureHeight);
    TextureTarget->UpdateResourceImmediate(true);
}
