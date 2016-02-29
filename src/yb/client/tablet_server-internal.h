// Licensed to the Apache Software Foundation (ASF) under one
// or more contributor license agreements.  See the NOTICE file
// distributed with this work for additional information
// regarding copyright ownership.  The ASF licenses this file
// to you under the Apache License, Version 2.0 (the
// "License"); you may not use this file except in compliance
// with the License.  You may obtain a copy of the License at
//
//   http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing,
// software distributed under the License is distributed on an
// "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
// KIND, either express or implied.  See the License for the
// specific language governing permissions and limitations
// under the License.
#ifndef KUDU_CLIENT_TABLET_SERVER_INTERNAL_H
#define KUDU_CLIENT_TABLET_SERVER_INTERNAL_H

#include <string>

#include "yb/client/client.h"
#include "yb/gutil/macros.h"

namespace yb {
namespace client {

class KuduTabletServer::Data {
 public:
  Data(std::string uuid, std::string hostname);
  ~Data();

  const std::string uuid_;
  const std::string hostname_;

  DISALLOW_COPY_AND_ASSIGN(Data);
};

} // namespace client
} // namespace yb

#endif