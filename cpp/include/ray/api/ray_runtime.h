// Copyright 2020-2021 The Ray Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//  http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#pragma once

#include <ray/api/function_manager.h>
#include <ray/api/task_options.h>

#include <cstdint>
#include <memory>
#include <msgpack.hpp>
#include <typeinfo>
#include <vector>

namespace ray {
namespace internal {

struct RemoteFunctionHolder {
  RemoteFunctionHolder() = default;
  template <typename F>
  RemoteFunctionHolder(F func) {
    auto func_name = FunctionManager::Instance().GetFunctionName(func);
    if (func_name.empty()) {
      throw RayException(
          "Function not found. Please use RAY_REMOTE to register this function.");
    }
    function_name = std::move(func_name);
  }

  /// The remote function name.
  std::string function_name;
};

class RayRuntime {
 public:
  virtual std::string Put(std::shared_ptr<msgpack::sbuffer> data) = 0;
  virtual std::shared_ptr<msgpack::sbuffer> Get(const std::string &id) = 0;

  virtual std::vector<std::shared_ptr<msgpack::sbuffer>> Get(
      const std::vector<std::string> &ids) = 0;

  virtual std::vector<bool> Wait(const std::vector<std::string> &ids, int num_objects,
                                 int timeout_ms) = 0;

  virtual std::string Call(const RemoteFunctionHolder &remote_function_holder,
                           std::vector<TaskArg> &args,
                           const CallOptions &task_options) = 0;
  virtual std::string CreateActor(const RemoteFunctionHolder &remote_function_holder,
                                  std::vector<TaskArg> &args,
                                  const ActorCreationOptions &create_options) = 0;
  virtual std::string CallActor(const RemoteFunctionHolder &remote_function_holder,
                                const std::string &actor, std::vector<TaskArg> &args,
                                const CallOptions &call_options) = 0;
  virtual void AddLocalReference(const std::string &id) = 0;
  virtual void RemoveLocalReference(const std::string &id) = 0;
  virtual std::string GetActorId(bool global, const std::string &actor_name) = 0;
  virtual void KillActor(const std::string &str_actor_id, bool no_restart) = 0;
  virtual void ExitActor() = 0;
  virtual ray::PlacementGroup CreatePlacementGroup(
      const ray::internal::PlacementGroupCreationOptions &create_options) = 0;
  virtual void RemovePlacementGroup(const std::string &group_id) = 0;
  virtual bool WaitPlacementGroupReady(const std::string &group_id,
                                       int timeout_seconds) = 0;
  virtual bool WasCurrentActorRestarted() = 0;
};
}  // namespace internal
}  // namespace ray