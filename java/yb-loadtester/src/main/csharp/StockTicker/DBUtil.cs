// Copyright (c) YugaByte, Inc.

using Cassandra;
using System;
using System.Collections.Generic;
using System.Net;
using System.Linq;
using System.Collections.Concurrent;
using System.Threading.Tasks;

namespace YB
{
  public class DBUtil
  {
    readonly String KeyspaceName = "ybdemo";
    protected List<IPEndPoint> hostIpAndPorts = new List<IPEndPoint> ();
    protected ISession dbSession;
    protected Cluster dbCluster;
    private readonly ConcurrentDictionary<string, Lazy<Task<PreparedStatement>>> _cachedStatements;

    public DBUtil (List<String> nodes)
    {
      nodes.ForEach ((node) => hostIpAndPorts.Add (ParseIPEndPoint (node)));
      dbCluster = Cluster.Builder ().AddContactPoints (hostIpAndPorts).Build ();
      dbSession = Connect ();
      _cachedStatements = new ConcurrentDictionary<string, Lazy<Task<PreparedStatement>>> ();
    }

    private static IPEndPoint ParseIPEndPoint (string node)
    {
      if (Uri.TryCreate (node, UriKind.Absolute, out Uri uri))
        return new IPEndPoint (IPAddress.Parse (uri.Host), uri.Port < 0 ? 0 : uri.Port);
      if (Uri.TryCreate ($"tcp://{node}", UriKind.Absolute, out uri))
        return new IPEndPoint (IPAddress.Parse (uri.Host), uri.Port < 0 ? 0 : uri.Port);
      if (Uri.TryCreate ($"tcp://[{node}]", UriKind.Absolute, out uri))
        return new IPEndPoint (IPAddress.Parse (uri.Host), uri.Port < 0 ? 0 : uri.Port);
      throw new FormatException ("Failed to parse text to IPEndPoint");
    }

    public Task<PreparedStatement> GetOrAddQuery (string cql)
    {
      return _cachedStatements.GetOrAdd (cql, CreateQueryTask).Value;
    }

    private Lazy<Task<PreparedStatement>> CreateQueryTask (string cql)
    {
      return new Lazy<Task<PreparedStatement>> (() => dbSession.PrepareAsync (cql));
    }

    ISession Connect ()
    {
      if (dbSession == null) {
        Console.WriteLine ("Connect To Cluster");
        try {
          dbSession = dbCluster.Connect (KeyspaceName);
        } catch (InvalidQueryException e) {
          if (e.Message.Contains ("Keyspace Not Found")) {
            Console.WriteLine ($"Keyspace {KeyspaceName} not found creating..");
            CreateKeyspaceAndConnect ();
          }
        }

      }
      return dbSession;
    }

    public void Disconnect()
    {
      Console.WriteLine ("Disconnect from Cluster");
      dbCluster.Shutdown ();
    }

    public RowSet ExecuteQuery (String queryStr)
    {
      return dbSession.Execute (queryStr);
    }

    public RowSet ExecuteQuery (BoundStatement statement)
    {
      return dbSession.Execute (statement);
    }

    private void CreateKeyspaceAndConnect()
    {
      dbSession = dbCluster.Connect ();
      dbSession.CreateKeyspace (KeyspaceName, new Dictionary<string, string> {
          { "class", "SimpleStrategy" },
          { "replication_factor", "3" }
      });
      dbSession = dbCluster.Connect (KeyspaceName);
    }

    public void DropTable (String TableName)
    {
      Console.WriteLine ("Dropping Stock Ticker table {0}", TableName);
      String statementStr = $"DROP TABLE IF EXISTS {TableName}";
      dbSession.Execute (statementStr);
    }

    public bool CheckTableExists (String TableName)
    {
      String statementStr = "SELECT table_name FROM system_schema.tables " +
                            " WHERE keyspace_name = ? AND table_name = ?";
      BoundStatement stmt = dbSession.Prepare(statementStr)
                                     .Bind (KeyspaceName, TableName);
      Row result = dbSession.Execute (stmt).FirstOrDefault ();
      if (result != null) {
        Console.WriteLine ("Stock Ticker table {0} exits.", result ["table_name"]);
        return true;
      }
      return false;
    }
  }
}