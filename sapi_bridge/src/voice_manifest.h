#pragma once

#include <windows.h>
#include <string>
#include <vector>

struct VoiceEntry
{
    std::wstring id, name, gender, lang, age, model;
};

std::vector<VoiceEntry> LoadVoices(DWORD &port) noexcept;
