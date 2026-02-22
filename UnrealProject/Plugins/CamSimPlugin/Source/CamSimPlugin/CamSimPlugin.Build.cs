using UnrealBuildTool;

public class CamSimPlugin : ModuleRules
{
	public CamSimPlugin(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = ModuleRules.PCHUsageMode.UseExplicitOrSharedPCHs;

		PublicIncludePaths.AddRange(new string[] { });
		PrivateIncludePaths.AddRange(new string[] { });

		PublicDependencyModuleNames.AddRange(new string[]
		{
			"Core",
			"CoreUObject",
			"Engine",
			"InputCore",
			"Networking",
			"Sockets",
			"RenderCore",
			"RHI",
			"Renderer",
			// Cesium for Unreal
			"CesiumRuntime",
		});

		PrivateDependencyModuleNames.AddRange(new string[]
		{
			"Slate",
			"SlateCore",
		});

		// POSIX shared memory — available on Linux and macOS
		if (Target.Platform == UnrealTargetPlatform.Linux || Target.Platform == UnrealTargetPlatform.Mac)
		{
			PrivateDependencyModuleNames.Add("Core");
		}

		// Windows shared memory via CreateFileMapping / MapViewOfFile — no extra module needed.
		if (Target.Platform == UnrealTargetPlatform.Win64)
		{
			PublicSystemLibraries.Add("kernel32.lib");
		}
	}
}
