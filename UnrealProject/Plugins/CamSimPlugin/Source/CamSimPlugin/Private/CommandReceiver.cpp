#include "CommandReceiver.h"
#include "AircraftKinematicActor.h"
#include "GimbalComponent.h"

#include "Sockets.h"
#include "SocketSubsystem.h"
#include "IPAddress.h"
#include "Interfaces/IPv4/IPv4Address.h"
#include "Interfaces/IPv4/IPv4Endpoint.h"

// ---------------------------------------------------------------------------
// Helpers to read little-endian values from a byte buffer
// ---------------------------------------------------------------------------

static float ReadF32LE(const uint8* p)
{
    uint32 Raw = (uint32)p[0] | ((uint32)p[1] << 8) | ((uint32)p[2] << 16) | ((uint32)p[3] << 24);
    float Val;
    FMemory::Memcpy(&Val, &Raw, 4);
    return Val;
}

static double ReadF64LE(const uint8* p)
{
    uint64 Raw = 0;
    for (int i = 0; i < 8; i++) Raw |= ((uint64)p[i] << (8 * i));
    double Val;
    FMemory::Memcpy(&Val, &Raw, 8);
    return Val;
}

// ---------------------------------------------------------------------------

UCommandReceiver::UCommandReceiver()
{
    PrimaryComponentTick.bCanEverTick = true;
    PrimaryComponentTick.TickGroup    = TG_PrePhysics;
}

UCommandReceiver::~UCommandReceiver()
{
    CloseSocket();
}

void UCommandReceiver::SetTargets(AAircraftKinematicActor* InAircraft, UGimbalComponent* InGimbal)
{
    AircraftTarget = InAircraft;
    GimbalTarget   = InGimbal;
}

void UCommandReceiver::BeginPlay()
{
    Super::BeginPlay();
    OpenSocket();
}

void UCommandReceiver::EndPlay(const EEndPlayReason::Type Reason)
{
    CloseSocket();
    Super::EndPlay(Reason);
}

void UCommandReceiver::TickComponent(float DeltaTime, ELevelTick TickType,
                                      FActorComponentTickFunction* ThisTickFunction)
{
    Super::TickComponent(DeltaTime, TickType, ThisTickFunction);
    DrainSocket();
}

// ---------------------------------------------------------------------------
// Socket management
// ---------------------------------------------------------------------------

bool UCommandReceiver::OpenSocket()
{
    ISocketSubsystem* SocketSub = ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM);
    if (!SocketSub)
    {
        UE_LOG(LogTemp, Error, TEXT("CamSim CommandReceiver: no socket subsystem"));
        return false;
    }

    Socket = SocketSub->CreateSocket(NAME_DGram, TEXT("CamSimCmd"), false);
    if (!Socket)
    {
        UE_LOG(LogTemp, Error, TEXT("CamSim CommandReceiver: CreateSocket failed"));
        return false;
    }

    Socket->SetNonBlocking(true);
    Socket->SetReuseAddr(true);

    TSharedRef<FInternetAddr> BindAddr = SocketSub->CreateInternetAddr();
    BindAddr->SetAnyAddress();
    BindAddr->SetPort(UDPPort);

    if (!Socket->Bind(*BindAddr))
    {
        UE_LOG(LogTemp, Error, TEXT("CamSim CommandReceiver: bind on port %d failed"), UDPPort);
        CloseSocket();
        return false;
    }

    UE_LOG(LogTemp, Log, TEXT("CamSim CommandReceiver: listening on UDP port %d"), UDPPort);
    return true;
}

void UCommandReceiver::CloseSocket()
{
    if (Socket)
    {
        Socket->Close();
        ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM)->DestroySocket(Socket);
        Socket = nullptr;
    }
}

// ---------------------------------------------------------------------------
// Receive loop
// ---------------------------------------------------------------------------

void UCommandReceiver::DrainSocket()
{
    if (!Socket) return;

    static uint8 RecvBuf[256];

    uint32 PendingSize = 0;
    while (Socket->HasPendingData(PendingSize))
    {
        int32 BytesRead = 0;
        TSharedRef<FInternetAddr> SenderAddr = ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM)->CreateInternetAddr();

        bool bOk = Socket->RecvFrom(RecvBuf, sizeof(RecvBuf), BytesRead, *SenderAddr);
        if (!bOk || BytesRead < static_cast<int32>(CMD_HDR_SIZE)) continue;

        // Parse header (little-endian)
        uint32 Magic      = (uint32)RecvBuf[0] | ((uint32)RecvBuf[1] << 8)
                          | ((uint32)RecvBuf[2] << 16) | ((uint32)RecvBuf[3] << 24);
        if (Magic != CMD_MAGIC) continue;

        uint8  MsgType    = RecvBuf[4];
        // RecvBuf[5] = reserved
        uint16 PayloadLen = (uint16)RecvBuf[6] | ((uint16)RecvBuf[7] << 8);

        if (static_cast<int32>(CMD_HDR_SIZE + PayloadLen) > BytesRead) continue;

        DispatchCommand(static_cast<ECamSimCmd>(MsgType),
                        RecvBuf + CMD_HDR_SIZE, PayloadLen);
    }
}

void UCommandReceiver::DispatchCommand(ECamSimCmd Type, const uint8* Payload, uint16 PayloadLen)
{
    switch (Type)
    {
    case ECamSimCmd::SlewPan:
        if (PayloadLen >= 4 && GimbalTarget)
            GimbalTarget->SetSlewRates(ReadF32LE(Payload), 0.0f);
        break;

    case ECamSimCmd::SlewTilt:
        if (PayloadLen >= 4 && GimbalTarget)
            GimbalTarget->SetSlewRates(0.0f, ReadF32LE(Payload));
        break;

    case ECamSimCmd::SlewBoth:
        if (PayloadLen >= 8 && GimbalTarget)
            GimbalTarget->SetSlewRates(ReadF32LE(Payload), ReadF32LE(Payload + 4));
        break;

    case ECamSimCmd::SetPosition:
        if (PayloadLen >= 20 && AircraftTarget)
        {
            double Lat = ReadF64LE(Payload);
            double Lon = ReadF64LE(Payload + 8);
            float  Alt = ReadF32LE(Payload + 16);
            AircraftTarget->HandleSetPosition(Lat, Lon, Alt);
        }
        break;

    case ECamSimCmd::SetHeading:
        if (PayloadLen >= 4 && AircraftTarget)
            AircraftTarget->HandleSetHeading(ReadF32LE(Payload));
        break;

    case ECamSimCmd::SetSpeed:
        if (PayloadLen >= 4 && AircraftTarget)
            AircraftTarget->HandleSetSpeed(ReadF32LE(Payload));
        break;

    case ECamSimCmd::SetGimbalAbs:
        if (PayloadLen >= 8 && GimbalTarget)
            GimbalTarget->SetAbsolutePosition(ReadF32LE(Payload), ReadF32LE(Payload + 4));
        break;

    case ECamSimCmd::Ping:
        UE_LOG(LogTemp, Verbose, TEXT("CamSim CommandReceiver: Ping received"));
        break;

    default:
        UE_LOG(LogTemp, Warning, TEXT("CamSim CommandReceiver: unknown cmd 0x%02X"), (uint8)Type);
        break;
    }
}
