#pragma once

#include "CoreMinimal.h"
#include "Components/SceneCaptureComponent2D.h"
#include "CesiumGeoreference.h"
#include "SimCameraComponent.generated.h"

class UGimbalComponent;

/**
 * USimCameraComponent
 *
 * Wraps USceneCaptureComponent2D to provide gimbal-stabilised camera capture.
 *
 * Each tick the component:
 *   1. Queries GimbalComponent for current world rotation.
 *   2. Updates the SceneCapture orientation.
 *   3. Optionally casts a downward line-trace to compute slant range and
 *      frame-centre ground coordinates.
 *
 * The captured texture is read back via FrameExporter, not here.
 */
UCLASS(ClassGroup = "CamSim", meta = (BlueprintSpawnableComponent))
class CAMSIMPLUGIN_API USimCameraComponent : public USceneCaptureComponent2D
{
    GENERATED_BODY()

public:
    USimCameraComponent();

    // -----------------------------------------------------------------------
    // Configuration
    // -----------------------------------------------------------------------

    /** Horizontal field-of-view in degrees */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "CamSim|Camera")
    float HFovDeg = 40.0f;

    /** Vertical field-of-view derived from HFoV and aspect ratio (16:9) */
    float GetVFovDeg() const;

    // -----------------------------------------------------------------------
    // State updated each tick
    // -----------------------------------------------------------------------

    UPROPERTY(BlueprintReadOnly, Category = "CamSim|Camera")
    float SlantRangeM = 0.0f;

    UPROPERTY(BlueprintReadOnly, Category = "CamSim|Camera")
    double FrameCenterLatDeg = 0.0;

    UPROPERTY(BlueprintReadOnly, Category = "CamSim|Camera")
    double FrameCenterLonDeg = 0.0;

    UPROPERTY(BlueprintReadOnly, Category = "CamSim|Camera")
    float FrameCenterElevM = 0.0f;

    // -----------------------------------------------------------------------
    // Capture resolution
    // -----------------------------------------------------------------------

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "CamSim|Camera")
    int32 CaptureWidth = 1920;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "CamSim|Camera")
    int32 CaptureHeight = 1080;

    // -----------------------------------------------------------------------
    // Wiring
    // -----------------------------------------------------------------------

    /** Set by AircraftKinematicActor after construction */
    void SetGimbalComponent(UGimbalComponent* InGimbal);

    void SetCesiumGeoreference(ACesiumGeoreference* InRef);

    virtual void TickComponent(float DeltaTime, ELevelTick TickType,
                                FActorComponentTickFunction* ThisTickFunction) override;

protected:
    virtual void BeginPlay() override;

private:
    UPROPERTY()
    TObjectPtr<UGimbalComponent> GimbalComp;

    UPROPERTY()
    TObjectPtr<ACesiumGeoreference> CesiumRef;

    /** Update slant range and frame centre via line trace */
    void UpdateGroundPoint();

    /** Allocate / reallocate the render target at CaptureWidth × CaptureHeight */
    void EnsureRenderTarget();
};
