#include "AircraftKinematicActor.h"
#include "GimbalComponent.h"
#include "SimCameraComponent.h"
#include "CommandReceiver.h"
#include "FrameExporter.h"
#include "TelemetryExporter.h"

#include "CesiumGeoreference.h"
#include "CesiumWgs84Ellipsoid.h"

#include "Kismet/GameplayStatics.h"
#include "Math/UnrealMathUtility.h"

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

static constexpr double EarthRadiusM   = 6378137.0;
static constexpr double KnotsToMetersPerSec = 0.51444;
static constexpr double DegToRad       = PI / 180.0;
static constexpr double RadToDeg       = 180.0 / PI;

// ---------------------------------------------------------------------------
// Constructor
// ---------------------------------------------------------------------------

AAircraftKinematicActor::AAircraftKinematicActor()
{
    PrimaryActorTick.bCanEverTick = true;
    PrimaryActorTick.bStartWithTickEnabled = true;

    // Create sub-components
    GimbalComponent = CreateDefaultSubobject<UGimbalComponent>(TEXT("GimbalComponent"));
    CameraComponent = CreateDefaultSubobject<USimCameraComponent>(TEXT("SimCameraComponent"));
    CommandReceiverComponent  = CreateDefaultSubobject<UCommandReceiver>(TEXT("CommandReceiver"));
    FrameExporterComponent    = CreateDefaultSubobject<UFrameExporter>(TEXT("FrameExporter"));
    TelemetryExporterComponent = CreateDefaultSubobject<UTelemetryExporter>(TEXT("TelemetryExporter"));

    // SceneCapture must be attached to root to get position
    RootComponent = CreateDefaultSubobject<USceneComponent>(TEXT("RootComponent"));
    CameraComponent->SetupAttachment(RootComponent);
}

// ---------------------------------------------------------------------------
// BeginPlay
// ---------------------------------------------------------------------------

void AAircraftKinematicActor::BeginPlay()
{
    Super::BeginPlay();

    // Initialize runtime state from editor properties
    CurrentLatDeg     = InitialLatDeg;
    CurrentLonDeg     = InitialLonDeg;
    CurrentAltMHAE    = InitialAltMHAE;
    CurrentHeadingDeg = InitialHeadingDeg;

    // Find Cesium georeference in the level
    TArray<AActor*> FoundActors;
    UGameplayStatics::GetAllActorsOfClass(GetWorld(), ACesiumGeoreference::StaticClass(), FoundActors);
    if (FoundActors.Num() > 0)
    {
        CesiumGeoreference = Cast<ACesiumGeoreference>(FoundActors[0]);
    }
    else
    {
        UE_LOG(LogTemp, Warning, TEXT("CamSim: No CesiumGeoreference found in level — terrain will not be positioned correctly."));
    }

    // Wire sub-components
    CommandReceiverComponent->SetTargets(this, GimbalComponent);
    CameraComponent->SetGimbalComponent(GimbalComponent);
    CameraComponent->SetCesiumGeoreference(CesiumGeoreference);
    FrameExporterComponent->SetCameraComponent(CameraComponent);
    TelemetryExporterComponent->SetSources(this, GimbalComponent, CameraComponent);

    // Set initial world transform
    SyncWorldTransform();
}

// ---------------------------------------------------------------------------
// Tick
// ---------------------------------------------------------------------------

void AAircraftKinematicActor::Tick(float DeltaTime)
{
    Super::Tick(DeltaTime);

    AdvancePosition(DeltaTime);
    SyncWorldTransform();
}

// ---------------------------------------------------------------------------
// UDP command handlers
// ---------------------------------------------------------------------------

void AAircraftKinematicActor::HandleSetPosition(double LatDeg, double LonDeg, float AltMHAE)
{
    CurrentLatDeg  = LatDeg;
    CurrentLonDeg  = LonDeg;
    CurrentAltMHAE = static_cast<double>(AltMHAE);
}

void AAircraftKinematicActor::HandleSetHeading(float HeadingDeg)
{
    CurrentHeadingDeg = FMath::Fmod(HeadingDeg, 360.0f);
    if (CurrentHeadingDeg < 0.0f) CurrentHeadingDeg += 360.0f;
}

void AAircraftKinematicActor::HandleSetSpeed(float InSpeedKts)
{
    SpeedKts = InSpeedKts;
}

// ---------------------------------------------------------------------------
// Private helpers
// ---------------------------------------------------------------------------

void AAircraftKinematicActor::AdvancePosition(float DeltaTime)
{
    // Simple haversine dead-reckoning on WGS-84 sphere approximation.
    // Distance advanced this tick in metres:
    const double SpeedMps = static_cast<double>(SpeedKts) * KnotsToMetersPerSec;
    const double DistM    = SpeedMps * static_cast<double>(DeltaTime);

    // Angular distance on the sphere
    const double AngularDist = DistM / EarthRadiusM;

    const double LatRad = CurrentLatDeg * DegToRad;
    const double LonRad = CurrentLonDeg * DegToRad;
    const double HdgRad = static_cast<double>(CurrentHeadingDeg) * DegToRad;

    const double NewLat = FMath::Asin(
        FMath::Sin(LatRad) * FMath::Cos(AngularDist) +
        FMath::Cos(LatRad) * FMath::Sin(AngularDist) * FMath::Cos(HdgRad));

    const double NewLon = LonRad + FMath::Atan2(
        FMath::Sin(HdgRad) * FMath::Sin(AngularDist) * FMath::Cos(LatRad),
        FMath::Cos(AngularDist) - FMath::Sin(LatRad) * FMath::Sin(NewLat));

    CurrentLatDeg = NewLat * RadToDeg;
    CurrentLonDeg = NewLon * RadToDeg;

    // Normalise longitude to [-180, 180]
    while (CurrentLonDeg >  180.0) CurrentLonDeg -= 360.0;
    while (CurrentLonDeg < -180.0) CurrentLonDeg += 360.0;
}

void AAircraftKinematicActor::SyncWorldTransform()
{
    if (!CesiumGeoreference) return;

    // Convert geodetic → Unreal world coordinates
    const FVector UnrealPos = CesiumGeoreference->TransformLongitudeLatitudeHeightPositionToUnreal(
        FVector(CurrentLonDeg, CurrentLatDeg, CurrentAltMHAE));

    // Build aircraft orientation: heading is yaw (ENU → UE axes handled by Cesium)
    // Cesium ENU: X=East, Y=North, Z=Up.  Unreal: X=Forward, Y=Right, Z=Up.
    // We express heading as yaw in the Unreal coordinate system at this geo location.
    const FRotator AircraftRot(
        static_cast<double>(PlatformPitchDeg),   // pitch
        static_cast<double>(CurrentHeadingDeg),  // yaw
        static_cast<double>(PlatformRollDeg));    // roll

    SetActorLocationAndRotation(UnrealPos, AircraftRot);
}
