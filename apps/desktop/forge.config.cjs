const path = require("node:path");

const projectRoot = path.resolve(__dirname, "../..");
const platformIcon = process.platform === "win32"
  ? path.join(projectRoot, "assets/branding/aiive-app-icon.ico")
  : process.platform === "darwin"
    ? path.join(projectRoot, "assets/branding/aiive-app-icon.icns")
    : path.join(projectRoot, "assets/branding/aiive-app-icon.png");

module.exports = {
  packagerConfig: {
    asar: { unpack: "src/node-host/**" },
    icon: platformIcon,
    extraResource: [
      path.join(projectRoot, "assets/branding/aiive-app-icon.png"),
    ],
  },
  rebuildConfig: {},
  makers: [
    {
      name: "@electron-forge/maker-squirrel",
      config: {
        name: "aiive",
        setupIcon: path.join(projectRoot, "assets/branding/aiive-app-icon.ico"),
      },
    },
    { name: "@electron-forge/maker-zip", platforms: ["darwin", "win32"] },
    {
      name: "@electron-forge/maker-deb",
      config: { options: { icon: path.join(projectRoot, "assets/branding/aiive-app-icon.png") } },
    },
    {
      name: "@electron-forge/maker-rpm",
      config: { options: { icon: path.join(projectRoot, "assets/branding/aiive-app-icon.png") } },
    },
  ],
};
